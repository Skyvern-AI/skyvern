from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.build_phase import BuildPhase
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.output_policy import (
    CopilotOutputKind,
    OutputPolicyReason,
    OutputPolicyVerdict,
    _contains_internal_tool_vocab_leak,
    derive_output_kind,
    evaluate_output_policy,
    hard_block_output_policy_verdict,
    normalize_response_scaffolding,
)
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentMode


def _credential(credential_id: str = "cred_safe", tested_url: str = "https://login.example.test/login") -> object:
    return SimpleNamespace(credential_id=credential_id, name="Saved Login", tested_url=tested_url)


def _policy(**overrides: object) -> RequestPolicy:
    defaults = dict(resolved_credentials=[_credential()])
    defaults.update(overrides)
    return RequestPolicy(**defaults)


def _ctx(**overrides: object) -> CopilotContext:
    defaults = dict(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
        request_policy=_policy(),
    )
    defaults.update(overrides)
    return CopilotContext(**defaults)


def _fake_run_result(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(final_output=json.dumps(payload), new_items=[])


def _chat_request() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_copilot_chat_id="chat-1",
        workflow_yaml="title: Prior",
    )


def _workflow_yaml(
    *,
    url: str = "https://login.example.test/login",
    navigation_goal: str = "Log in.",
) -> str:
    return f"""
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: login_credentials
      default_value: cred_safe
  blocks:
    - block_type: login
      label: login
      url: {url}
      navigation_goal: {navigation_goal}
      parameter_keys:
        - login_credentials
"""


def _code_artifact_metadata(label: str = "open_results") -> list[dict[str, object]]:
    return [
        {
            "block_label": label,
            "declared_goal": "Open the result page.",
            "claimed_outcomes": [
                {
                    "id": "claim:open_results",
                    "scope": "sufficient_for_prefix_validation",
                    "text": "The result page is reachable.",
                    "status": "satisfied",
                    "depends_on": ["dep:result_link"],
                    "criteria_ids": ["criterion:result_page_reached"],
                    "evidence_refs": ["evidence:result_link"],
                }
            ],
            "page_dependencies": [
                {
                    "id": "dep:result_link",
                    "scope": "current_page_state",
                    "status": "satisfied",
                    "evidence_refs": ["evidence:result_link"],
                }
            ],
            "completion_criteria": [
                {
                    "id": "criterion:result_page_reached",
                    "text": "The result page is visible after authored execution.",
                    "level": "terminal",
                }
            ],
            "evidence_refs": [
                {
                    "evidence_ref": "evidence:result_link",
                    "claim_id": "claim:open_results",
                    "dependency_id": "dep:result_link",
                    "criterion_id": "criterion:result_page_reached",
                    "status": "satisfied",
                    "source_tool": "inspect_page_for_composition",
                    "observation_step": 1,
                }
            ],
            "observation_refs": [],
            "terminal_verifier_expectations": [
                {
                    "id": "verify:result_page_reached",
                    "text": "Verify the result page is visible.",
                    "criteria_ids": ["criterion:result_page_reached"],
                    "claimed_outcome_ids": ["claim:open_results"],
                }
            ],
            "exploration_observations": [],
        }
    ]


def _inline_conditional_workflow_yaml(*, url: str = "https://login.example.test/login") -> str:
    return f"""
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: login_credentials
      default_value: cred_safe
  blocks:
    - block_type: conditional
      label: route_login
      branch_conditions:
        - is_default: true
          blocks:
            - block_type: login
              label: login
              url: {url}
              parameter_keys:
                - login_credentials
"""


def _nested_branch_workflow_yaml(*, url: str = "https://login.example.test/login") -> str:
    return f"""
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: login_credentials
      default_value: cred_safe
  blocks:
    - block_type: conditional
      label: route_login
      branch_conditions:
        - is_default: true
          branch_conditions:
            - is_default: true
              blocks:
                - block_type: login
                  label: nested_login
                  url: {url}
                  parameter_keys:
                    - login_credentials
"""


def test_rejects_raw_secret_echo_in_user_response() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response="I used password: hunter2 to test the login.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes


@pytest.mark.parametrize(
    ("response_type", "user_response", "expected_type", "expected_response"),
    [
        ("REPLY", "ASK_QUESTION\nWhich account should I use?", "ASK_QUESTION", "Which account should I use?"),
        ("REPLY", "  ASK_QUESTION\nWhich account should I use?", "ASK_QUESTION", "Which account should I use?"),
        ("REPLY", "ASK_QUESTION Which account should I use?", "ASK_QUESTION", "Which account should I use?"),
        ("REPLY", "ask_question\nWhich account should I use?", "ASK_QUESTION", "Which account should I use?"),
        ("REPLY", "Ask_Question Which account should I use?", "ASK_QUESTION", "Which account should I use?"),
        ("REPLY", "REPLY\nI can help with that.", "REPLY", "I can help with that."),
        ("REPLY", "reply\nI can help with that.", "REPLY", "I can help with that."),
        ("REPLY", "REPLACE_WORKFLOW\nI updated the workflow.", "REPLY", "I updated the workflow."),
        ("REPLY", "REPLACE_WORKFLOW I updated the workflow.", "REPLY", "I updated the workflow."),
        (
            "REPLACE_WORKFLOW",
            "REPLACE_WORKFLOW\nI updated the workflow.",
            "REPLACE_WORKFLOW",
            "I updated the workflow.",
        ),
    ],
)
def test_normalizes_plain_internal_response_label_scaffolding(
    response_type: str,
    user_response: str,
    expected_type: str,
    expected_response: str,
) -> None:
    normalized = normalize_response_scaffolding(response_type, user_response)

    assert normalized.changed
    assert normalized.response_type == expected_type
    assert normalized.user_response == expected_response


def test_normalize_scaffolding_preserves_ordinary_reply_sentence() -> None:
    text = "Reply with the invoice number from the page."
    normalized = normalize_response_scaffolding("REPLY", text)

    assert not normalized.changed
    assert normalized.response_type == "REPLY"
    assert normalized.user_response == text


def test_normalize_scaffolding_preserves_wrapped_ordinary_reply_sentence() -> None:
    text = "Reply with\nthe invoice number from the page."
    normalized = normalize_response_scaffolding("REPLY", text)

    assert not normalized.changed
    assert normalized.response_type == "REPLY"
    assert normalized.user_response == text


@pytest.mark.parametrize(
    "user_response",
    [
        "Next step: call get_run_results with this workflow_run_id.",
        'Call `get_run_results(workflow_run_id="wr_123")` first, then await user input.',
        "Then call update_and_run_blocks with a smaller chain.",
        "Do NOT retry this tool call; wait for the current run result.",
        "Do NOT re-invoke the tool until the user responds.",
    ],
)
def test_rejects_internal_tool_instruction_leak_in_user_response(user_response: str) -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=user_response,
    )

    assert not verdict.allowed
    assert OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK in verdict.reason_codes
    assert OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK in hard_block_output_policy_verdict(verdict).reason_codes


def test_allows_benign_do_not_retry_user_guidance() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="Do not retry until the account lockout clears.",
    )

    assert verdict.allowed
    assert OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK not in verdict.reason_codes


@pytest.mark.parametrize(
    "user_response",
    [
        "I used hunter2 as the password.",
        "Type hunter2 into the password field.",
    ],
)
def test_does_not_infer_arbitrary_prose_secret_echoes(user_response: str) -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response=user_response,
    )

    assert verdict.allowed


def test_rejects_raw_secret_in_workflow_yaml() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        workflow_yaml=_workflow_yaml(navigation_goal="Log in with password: hunter2."),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes


def test_rejects_raw_secret_in_structured_tool_arguments() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        tool_arguments={"workflow_yaml": {"navigation_goal": "Type password: hunter2 into the field."}},
    )

    assert not verdict.allowed
    assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes


class TestSanctionedSecretReferenceIdiom:
    """`password = parameters[...]` / attribute-chain reads are references to bound
    values, not leaks — the code-only authoring idiom must pass the syntactic
    backstop while literal values keep blocking."""

    def test_allows_parameters_subscript_assignment_in_tool_arguments(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            tool_arguments={"workflow_yaml": 'code: |\n  password = parameters["login_credential"]'},
        )

        assert verdict.allowed

    def test_allows_credential_attribute_read_in_workflow_yaml(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            workflow_yaml=(
                "code: |\n"
                "  username = login_credential.username\n"
                "  password = login_credential.password\n"
                '  await page.locator("#passwordInput").fill(login_credential.password)\n'
            ),
        )

        assert verdict.allowed

    def test_allows_str_wrapped_parameter_reference(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            tool_arguments={"workflow_yaml": 'code: |\n  token = str(parameters.get("api_token"))'},
        )

        assert verdict.allowed

    def test_still_rejects_literal_appended_to_reference(self) -> None:
        for rhs in (
            'login_credential.password+"hunter2"',
            'str(login_credential.password)+"hunter2"',
            'parameters["cred"]or"hunter2"',
        ):
            verdict = evaluate_output_policy(
                request_policy=_policy(),
                tool_arguments={"workflow_yaml": f"code: |\n  password = {rhs}"},
            )

            assert not verdict.allowed, rhs
            assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_allows_keyword_argument_reference_with_closing_paren(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            tool_arguments={"workflow_yaml": 'code: |\n  await do_login(password=parameters["cred"])'},
        )

        assert verdict.allowed

    def test_still_rejects_jwt_shaped_literal_assignment(self) -> None:
        # A dotted literal (JWT-shaped) must not pass as an "attribute chain".
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            tool_arguments={"workflow_yaml": "code: |\n  token = eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig"},
        )

        assert not verdict.allowed
        assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_still_rejects_dotted_literal_not_ending_in_credential_field(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            tool_arguments={"workflow_yaml": "code: |\n  password = admin.password123"},
        )

        assert not verdict.allowed
        assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_still_rejects_quoted_literal_assignment(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            tool_arguments={"workflow_yaml": 'code: |\n  password = "hunter2"'},
        )

        assert not verdict.allowed
        assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_still_rejects_bare_literal_assignment(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            tool_arguments={"workflow_yaml": "password: hunter2"},
        )

        assert not verdict.allowed
        assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_still_rejects_email_password_pair_next_to_reference_idiom(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            tool_arguments={
                "workflow_yaml": 'code: |\n  password = parameters["cred"]\nnotes: qa.user@example.test:FakePass123!'
            },
        )

        assert not verdict.allowed
        assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_chat_surface_request_policy_detection_is_unchanged(self) -> None:
        # The pre-agent chat-surface detector stays conservative: even the code
        # idiom pasted into chat still counts as a raw-secret signal there.
        from skyvern.forge.sdk.copilot.request_policy import _raw_secret_detected

        assert _raw_secret_detected("password: hunter2")
        assert _raw_secret_detected('password = parameters["cred"]')


def test_rejects_bulk_colon_delimited_credentials_in_tool_arguments() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        tool_arguments={
            "parameters": {"account_list": "alpha@example.test:FakePass123!\nbeta@example.test:AnotherFakePass456!"}
        },
    )

    assert not verdict.allowed
    assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes


def test_allows_scp_style_paths_and_url_ports_in_tool_arguments() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        tool_arguments={
            "repository": "git@github.com:skyvern-ai/skyvern.git",
            "local_url": "https://qa.user@example.test:8080?org=1",
        },
    )

    assert verdict.allowed


def test_does_not_hard_block_on_prior_global_context_secret() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response="I drafted a safe workflow.",
        global_llm_context='{"prior_note": "User previously pasted password: hunter2."}',
    )

    assert verdict.allowed


def test_rejects_raw_secret_even_when_workflow_contains_jinja_placeholder() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        workflow_yaml="navigation_goal: Use {{ parameters.username }} and password: hunter2.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes


def test_allows_sensitive_jinja_placeholder_in_navigation_goal() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        workflow_yaml=_workflow_yaml(
            navigation_goal="Type {{ parameters.password }} into the password field.",
        ),
    )

    assert verdict.allowed


def test_rejects_reply_when_request_policy_required_clarification() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(user_response_policy="ask_clarification", allow_update_workflow=False),
        response_type="REPLY",
        user_response="I created the workflow with the credential.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.REQUEST_POLICY_CLARIFICATION_BYPASS in verdict.reason_codes


def test_classifies_unbacked_workflow_delivery_claim() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="Here's the workflow.",
        has_workflow_proposal=False,
    )

    assert not verdict.allowed
    assert OutputPolicyReason.UNBACKED_WORKFLOW_DELIVERY_CLAIM in verdict.reason_codes
    assert OutputPolicyReason.MISSING_PROPOSAL_STATE in verdict.reason_codes


def test_flags_block_yaml_pasted_into_user_response() -> None:
    user_response = (
        "I've now updated the workflow to also accept the form's URL as a parameter, named `form_url`. "
        "Here's how the block now looks:\n\n"
        "    - label: navigate_and_fill_form\n"
        "      block_type: navigation\n"
        "      navigation_goal: Fill the abuse form using the supplied data.\n"
        '      url: "{{ form_url }}"\n'
        "      parameter_keys:\n"
        "        - name\n"
        "        - email\n"
        "        - form_url\n"
    )

    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=user_response,
        has_workflow_proposal=False,
    )

    assert not verdict.allowed
    assert OutputPolicyReason.WORKFLOW_YAML_IN_REPLY in verdict.reason_codes


def test_block_yaml_in_reply_is_not_hard_blocking() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="ASK_QUESTION",
        user_response=(
            "Here is the change I'd make:\n\n"
            "```yaml\n"
            "block_type: navigation\n"
            "navigation_goal: Submit the form.\n"
            "label: submit_form\n"
            "```\n"
        ),
        has_workflow_proposal=False,
    )

    assert OutputPolicyReason.WORKFLOW_YAML_IN_REPLY in verdict.reason_codes
    hard = hard_block_output_policy_verdict(verdict)
    assert OutputPolicyReason.WORKFLOW_YAML_IN_REPLY not in hard.reason_codes


def test_allows_workflow_delivery_language_after_failed_workflow_attempt() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="I created a draft workflow and tested it, but the test failed.",
        has_workflow_proposal=False,
        workflow_attempted=True,
    )

    assert verdict.allowed


def test_classifies_missing_unvalidated_proposal_affordance() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="I drafted the workflow but did not finish testing.",
        has_workflow_proposal=True,
        unvalidated=True,
    )

    assert not verdict.allowed
    assert OutputPolicyReason.MISSING_UNVALIDATED_PROPOSAL_AFFORDANCE in verdict.reason_codes


def test_rejects_incidental_accept_discard_language_for_unvalidated_proposal() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="I accept that the test failed, but I can't discard the login block without more info.",
        has_workflow_proposal=True,
        unvalidated=True,
    )

    assert not verdict.allowed
    assert OutputPolicyReason.MISSING_UNVALIDATED_PROPOSAL_AFFORDANCE in verdict.reason_codes


def test_allows_unvalidated_proposal_affordance_copy() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=(
            "Use Review to inspect it, Accept to save it, or Reject to discard it. "
            "It has not been tested or verified end-to-end."
        ),
        has_workflow_proposal=True,
        unvalidated=True,
    )

    assert verdict.allowed


def test_allows_backend_authored_request_policy_clarification() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(user_response_policy="ask_clarification", allow_update_workflow=False),
        response_type="ASK_QUESTION",
        user_response="Which saved credential should I use?",
    )

    assert verdict.allowed
    assert verdict.output_kind == CopilotOutputKind.CLARIFICATION_REQUEST


def test_rejects_deprecated_block_taxonomy_in_final_text() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=(
            "`task_v2` is deprecated. Use these newer block types instead: "
            "`navigation`, `extraction`, `validation`, `login`, `goto_url`, and `file_download`."
        ),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK in verdict.reason_codes


@pytest.mark.parametrize("deprecated_identifier", ["task_v2", "Task_V2", "task-v2", "task v2", "taskv2"])
def test_rejects_deprecated_block_identifier_outside_informational_answer(deprecated_identifier: str) -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        output_kind=CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL,
        user_response=f"The draft still contains `{deprecated_identifier}` from the older workflow.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK in verdict.reason_codes


def test_rejects_informational_block_taxonomy_list_without_deprecated_name() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=(
            "Use these block types: navigation for page actions, extraction for data, validation for checks, "
            "and goto_url for direct URLs."
        ),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK in verdict.reason_codes


def test_allows_two_internal_block_type_terms_in_informational_answer() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="Use a navigation block for the page action and an extraction block for the final data.",
    )

    assert verdict.allowed


def test_allows_generic_navigation_validation_extraction_prose() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=(
            "After login, the workflow uses navigation to reach the form, validation of the input, and "
            "extraction of the resulting data."
        ),
    )

    assert verdict.allowed


def test_allows_taxonomy_terms_outside_informational_answer_without_deprecated_identifier() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        output_kind=CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL,
        user_response=(
            "The draft includes navigation for page actions, extraction for data, validation for checks, "
            "and goto_url for direct URLs."
        ),
    )

    assert verdict.allowed


def test_allows_single_task_block_reference_without_deprecated_identifier() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=(
            "For that page action, use a navigation step as the task block and describe what the browser should do."
        ),
    )

    assert verdict.allowed
    assert OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK not in verdict.reason_codes


@pytest.mark.parametrize(
    "user_response",
    [
        "TurnIntent classified this turn as `edit`, but we couldn't proceed. safe_reason_code=turn_intent_no_mutation_run_blocked.",
        "Skipping: safe_reason_code=request_policy_clarification this turn.",
        "Blocked by `TurnIntent`: please rephrase.",
        "Hit turn_intent_unresolved_edit_target while routing.",
        "RequestPolicy blocked this turn for clarification.",
        "TurnIntent requires clarification before proceeding.",
        "Trace shows safe_reason_code = turn_intent_no_mutation_run_blocked at exit.",
        "Final safe_reason_code : request_policy_clarification reported.",
    ],
)
def test_rejects_internal_classifier_vocab_leak(user_response: str) -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=user_response,
    )

    assert OutputPolicyReason.INTERNAL_CLASSIFIER_VOCAB_LEAK in verdict.reason_codes


@pytest.mark.parametrize(
    "user_response",
    [
        "The request policy resolved your saved credential automatically.",
        "Use a turn-by-turn approach to walk through each step.",
        "I'll classify the turn as edit if you confirm.",
        "Need turnintent context? Tell me what you want to change.",
    ],
)
def test_allows_benign_prose_referencing_classifier_terms(user_response: str) -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=user_response,
    )

    assert OutputPolicyReason.INTERNAL_CLASSIFIER_VOCAB_LEAK not in verdict.reason_codes


@pytest.mark.parametrize(
    "user_response",
    [
        "Saw [copilot:nudge] in the trace before the failure.",
        "[copilot:screenshot] context truncated to keep this prompt small.",
        "LOOP DETECTED: same tool dispatched three times in a row.",
        "Loop Detected: same tool dispatched three times in a row.",
        "loop detected: same tool dispatched three times in a row.",
        "[Copilot:nudge] surfaced in the trace before the failure.",
        "[COPILOT:NUDGE] surfaced in the trace before the failure.",
        "Couldn't finish the diagnostic step on this nudge turn.",
    ],
)
def test_rejects_internal_machinery_vocab_via_extended_taxonomy(user_response: str) -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=user_response,
    )

    assert OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK in verdict.reason_codes


@pytest.mark.parametrize(
    "user_response",
    [
        'I\'ll call get_run_results(workflow_run_id="wr_123") and report back.',
        "Next I need to run_blocks_and_collect_debug[block_labels=['login']].",
        "Use `update_workflow` to apply that change.",
        "Trace shows get_run_results was the last tool dispatched on this turn.",
        "Falling back to list_credentials since the user did not specify one.",
    ],
)
def test_rejects_internal_tool_name_leak_via_tool_instruction(user_response: str) -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=user_response,
    )

    assert OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK in verdict.reason_codes
    assert OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK in hard_block_output_policy_verdict(verdict).reason_codes


@pytest.mark.parametrize(
    "user_response",
    [
        "I'll get the run results for you once it finishes.",
        "Update the workflow to log in before extracting data.",
        "List your saved credentials in the Credentials UI.",
        "I'll take a screenshot once the page loads.",
        "Nudge me if you want a different approach.",
    ],
)
def test_allows_benign_prose_referencing_tool_names(user_response: str) -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=user_response,
    )

    assert OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK not in verdict.reason_codes


@pytest.mark.parametrize(
    "user_response",
    [
        "Use `update_workflow` to apply that change.",
        "LOOP DETECTED: same tool dispatched three times in a row.",
        "Couldn't finish the diagnostic step on this nudge turn.",
        "Saw [copilot:nudge] in the trace before the failure.",
        "Trace shows get_run_results was the last tool dispatched on this turn.",
    ],
)
def test_extended_taxonomy_leak_skipped_on_ask_question(user_response: str) -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="ASK_QUESTION",
        user_response=user_response,
    )

    assert OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK not in verdict.reason_codes


@pytest.mark.parametrize(
    "output_kind",
    [
        CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL,
        CopilotOutputKind.WORKFLOW_UPDATE_PROPOSAL,
        CopilotOutputKind.WORKFLOW_RUN_RESULT,
    ],
)
def test_tool_name_leak_hard_blocks_across_output_kinds(
    output_kind: CopilotOutputKind,
) -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="Use `update_workflow` to apply that change.",
        output_kind=output_kind,
    )

    assert OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK in verdict.reason_codes


@pytest.mark.parametrize(
    "user_response",
    [
        "Send me a normal instruction like 'run it', and I'll continue.",
        "Reply 'yes' to proceed.",
        "Please send 'continue debugging' and I'll keep going.",
        "Type 'cancel' to stop the workflow.",
        "Kindly type 'cancel' to abort the current draft.",
        "   Reply 'yes' to proceed.",
        "\tReply 'yes' to proceed.",
    ],
)
def test_rejects_self_prescriptive_phrase(user_response: str) -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=user_response,
    )

    assert OutputPolicyReason.SELF_PRESCRIPTIVE_PHRASE_LEAK in verdict.reason_codes


@pytest.mark.parametrize(
    "user_response",
    [
        "I need the value for 'account_id' to continue.",
        "The user can reply 'yes' to confirm.",
        "I'll respond once I have 'account_id'.",
        "I'll save the workflow once you confirm.",
        "Please confirm whether to continue.",
        "I have a draft workflow proposal. I need this before I can build and test it: which credential should I use?",
        "Send me what you'd like changed and I'll update the workflow.",
        "Reply once you've reviewed the draft.",
        "Type the field name where you'd like the value populated.",
        "Respond once you've decided which credential to use.",
        'Type "navigate" in the action field of the block.',
        'Send "data" as JSON to the endpoint.',
        'Reply objects include a status field like "success" or "error".',
        "Reply 2's text is just a numeric ordinal, not a quote.",
    ],
)
def test_allows_benign_prose_around_quoted_examples(user_response: str) -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=user_response,
    )

    assert OutputPolicyReason.SELF_PRESCRIPTIVE_PHRASE_LEAK not in verdict.reason_codes


def test_rejects_unapproved_credential_id_in_final_text() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response="I used cred_other for the login.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes


def test_rejects_credential_id_when_request_policy_approved_no_credentials() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(resolved_credentials=[]),
        user_response="I used cred_some_existing for the login.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes


def test_allows_existing_workflow_credential_id_on_unrelated_turn() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            existing_workflow_credential_ids=["cred_safe"],
            existing_workflow_credential_origins={"cred_safe": ["https://login.example.test"]},
            credential_input_kind="none",
        ),
        workflow_yaml=_workflow_yaml(navigation_goal="Open the reports page."),
    )

    assert verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE not in verdict.reason_codes


def test_rejects_existing_workflow_credential_id_on_new_origin() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            existing_workflow_credential_ids=["cred_safe"],
            existing_workflow_credential_origins={"cred_safe": ["https://login.example.test"]},
            credential_input_kind="none",
        ),
        workflow_yaml=_workflow_yaml(url="https://evil.example.test/login"),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED in verdict.reason_codes


def test_rejects_existing_workflow_credential_id_on_inline_conditional_branch_new_origin() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            existing_workflow_credential_ids=["cred_safe"],
            existing_workflow_credential_origins={"cred_safe": ["https://login.example.test"]},
            credential_input_kind="none",
        ),
        workflow_yaml=_inline_conditional_workflow_yaml(url="https://evil.example.test/login"),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED in verdict.reason_codes


def test_rejects_existing_workflow_credential_id_on_nested_branch_new_origin() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            existing_workflow_credential_ids=["cred_safe"],
            existing_workflow_credential_origins={"cred_safe": ["https://login.example.test"]},
            credential_input_kind="none",
        ),
        workflow_yaml=_nested_branch_workflow_yaml(url="https://evil.example.test/login"),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED in verdict.reason_codes


def test_rejects_existing_workflow_credential_id_without_prior_origin_scope() -> None:
    # Existing workflow credentials with no tracked URL scope cannot safely
    # authorize later edits that introduce a credentialed URL.
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            existing_workflow_credential_ids=["cred_safe"],
            existing_workflow_credential_origins={},
            credential_input_kind="none",
        ),
        workflow_yaml=_workflow_yaml(url="https://login.example.test/login"),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED in verdict.reason_codes


def test_rejects_unapproved_credential_id_in_structured_tool_arguments() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        tool_arguments={"workflow_yaml": {"parameters": [{"default_value": "cred_other"}]}},
    )

    assert not verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes


def test_allows_explicit_unresolved_credential_id_for_untested_draft() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            credential_input_kind="credential_id",
            credential_refs=["cred_missing"],
            invalid_credential_ids=["cred_missing"],
            allow_missing_credentials_in_draft=True,
            allow_run_blocks=False,
        ),
        workflow_yaml=_workflow_yaml().replace("cred_safe", "cred_missing"),
    )

    assert verdict.allowed


def test_rejects_unrequested_credential_id_even_when_untested_draft_allows_missing_credentials() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            credential_input_kind="credential_name",
            credential_refs=["Missing Login"],
            allow_missing_credentials_in_draft=True,
            allow_run_blocks=False,
        ),
        workflow_yaml=_workflow_yaml().replace("cred_safe", "cred_other"),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes


def test_allows_approved_credential_id_in_workflow_yaml() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        workflow_yaml=_workflow_yaml(),
    )

    assert verdict.allowed


def test_rejects_approved_credential_on_different_login_origin() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        workflow_yaml=_workflow_yaml(url="https://evil.example.test/login"),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED in verdict.reason_codes


def test_rejects_approved_credential_on_templated_login_origin() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        workflow_yaml=_workflow_yaml(url='"{{ parameters.target_url }}"'),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED in verdict.reason_codes


def test_derive_output_kind_uses_state_over_response_type() -> None:
    assert (
        derive_output_kind(
            response_type="REPLY",
            request_policy=_policy(user_response_policy="ask_clarification"),
            updated_workflow=SimpleNamespace(),
            workflow_was_persisted=True,
            workflow_attempted=True,
            unvalidated=False,
        )
        == CopilotOutputKind.CLARIFICATION_REQUEST
    )
    assert (
        derive_output_kind(
            response_type="REPLY",
            request_policy=_policy(),
            updated_workflow=SimpleNamespace(),
            workflow_was_persisted=False,
            workflow_attempted=False,
            unvalidated=True,
        )
        == CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL
    )
    assert (
        derive_output_kind(
            response_type="REPLY",
            request_policy=_policy(),
            updated_workflow=SimpleNamespace(),
            workflow_was_persisted=True,
            workflow_attempted=True,
            unvalidated=False,
        )
        == CopilotOutputKind.WORKFLOW_RUN_RESULT
    )
    assert (
        derive_output_kind(
            response_type="REPLY",
            request_policy=_policy(),
            updated_workflow=None,
            workflow_was_persisted=True,
            workflow_attempted=False,
            unvalidated=False,
        )
        == CopilotOutputKind.INFORMATIONAL_ANSWER
    )


def test_plain_reply_after_prior_persist_stays_informational() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="I can help with that.",
        has_workflow_proposal=False,
        workflow_was_persisted=True,
        workflow_attempted=False,
    )

    assert verdict.allowed
    assert verdict.output_kind == CopilotOutputKind.INFORMATIONAL_ANSWER


def test_persistence_state_mismatch_is_a_hard_block() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response="I updated the workflow.",
        workflow_was_persisted=False,
        has_workflow_proposal=True,
        output_kind=CopilotOutputKind.WORKFLOW_UPDATE_PROPOSAL,
    )
    hard_verdict = hard_block_output_policy_verdict(verdict)

    assert not verdict.allowed
    assert OutputPolicyReason.PERSISTENCE_STATE_MISMATCH in verdict.reason_codes
    assert not hard_verdict.allowed
    assert hard_verdict.reason_codes == [OutputPolicyReason.PERSISTENCE_STATE_MISMATCH]


def test_output_policy_verdict_reason_codes_force_blocked_state() -> None:
    verdict = OutputPolicyVerdict(
        allowed=True,
        reason_codes=[OutputPolicyReason.REQUEST_POLICY_CLARIFICATION_BYPASS],
    )

    assert not verdict.allowed


def test_sdk_agent_guardrail_builders_use_policy_names() -> None:
    from agents import GuardrailFunctionOutput, InputGuardrail, OutputGuardrail

    input_guardrails = agent_module._build_copilot_input_guardrails(InputGuardrail, GuardrailFunctionOutput)
    output_guardrails = agent_module._build_copilot_output_guardrails(OutputGuardrail, GuardrailFunctionOutput)

    assert len(input_guardrails) == 1
    assert input_guardrails[0].name == "request_policy_guardrail"
    assert input_guardrails[0].run_in_parallel is False
    assert len(output_guardrails) == 1
    assert output_guardrails[0].name == "copilot_output_policy_guardrail"


@pytest.mark.asyncio
async def test_sdk_request_policy_guardrail_trips_on_required_clarification() -> None:
    from agents import GuardrailFunctionOutput, InputGuardrail
    from agents.run_context import RunContextWrapper

    input_guardrails = agent_module._build_copilot_input_guardrails(InputGuardrail, GuardrailFunctionOutput)
    result = await input_guardrails[0].run(
        SimpleNamespace(),
        "input",
        RunContextWrapper(
            context=_ctx(request_policy=_policy(user_response_policy="ask_clarification", allow_update_workflow=False))
        ),
    )

    assert result.output.tripwire_triggered is True
    assert result.output.output_info["user_response_policy"] == "ask_clarification"


def test_sdk_output_guardrail_hard_blocks_raw_secret_final_text() -> None:
    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        _ctx(),
        {"type": "REPLY", "user_response": "I used password: hunter2."},
    )

    assert response_type == "REPLY"
    assert not verdict.allowed
    assert verdict.reason_codes == [OutputPolicyReason.RAW_SECRET_LEAK]
    assert diagnostics == {
        "raw_output_kind": "informational_answer",
        "final_output_kind": "refusal",
        "raw_reason_codes": ["raw_secret_leak"],
        "hard_block_reason_codes": ["raw_secret_leak"],
        "soft_rewrite_reason_codes": [],
        "raw_would_have_failed": True,
        "contained_failure": True,
        "final_output_policy_allowed": False,
    }


@pytest.mark.parametrize(
    ("reason", "expected_terms"),
    [
        (OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE, ("credential", "confirm")),
        (OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED, ("credential", "url", "re-select")),
    ],
)
def test_output_policy_credential_block_asks_for_credential_confirmation(
    reason: OutputPolicyReason,
    expected_terms: tuple[str, ...],
) -> None:
    result = agent_module._build_output_policy_blocked_result(
        _ctx(),
        OutputPolicyVerdict(
            reason_codes=[reason],
        ),
        prior_global_llm_context="{}",
        prior_workflow_yaml="title: Prior",
    )

    assert result.response_type == "ASK_QUESTION"
    assert result.clear_proposed_workflow is False
    response = result.user_response.lower()
    for term in expected_terms:
        assert term in response
    assert "I could not safely return" not in result.user_response


def test_output_policy_block_preserves_already_gated_workflow_proposal() -> None:
    ctx = _ctx()
    ctx.last_workflow = SimpleNamespace(name="draft")
    ctx.last_workflow_yaml = "title: Draft"
    ctx.workflow_persisted = True
    ctx.last_test_ok = True

    result = agent_module._build_output_policy_blocked_result(
        ctx,
        OutputPolicyVerdict(
            reason_codes=[OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK],
        ),
        prior_global_llm_context="{}",
        prior_workflow_yaml="title: Prior",
    )

    assert result.response_type == "ASK_QUESTION"
    assert result.updated_workflow is ctx.last_workflow
    assert result.workflow_yaml == "title: Draft"
    assert result.workflow_was_persisted is True
    assert result.clear_proposed_workflow is False
    assert result.proposal_disposition == "review_tested"
    response = result.user_response.lower()
    assert "chat reply" in response
    assert "workflow draft" in response
    assert "saved" in response


def test_output_policy_specific_refusal_preserves_saved_draft_copy() -> None:
    ctx = _ctx()
    ctx.last_workflow = SimpleNamespace(name="draft")
    ctx.last_workflow_yaml = "title: Draft"

    result = agent_module._build_output_policy_blocked_result(
        ctx,
        OutputPolicyVerdict(
            reason_codes=[OutputPolicyReason.RAW_SECRET_LEAK],
        ),
        prior_global_llm_context="{}",
        prior_workflow_yaml="title: Prior",
    )

    assert result.updated_workflow is ctx.last_workflow
    assert result.clear_proposed_workflow is False
    assert result.proposal_disposition == "review_untested"
    assert "raw credentials or secrets" in result.user_response
    assert "chat reply" in result.user_response
    assert "workflow draft is still saved" in result.user_response


def test_scheduling_credential_policy_block_does_not_use_safety_refusal() -> None:
    scheduling_yaml = """
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: planning_credentials
      default_value: cred_unapproved
  blocks:
    - block_type: navigation
      label: export_current_month_time_clock_csv
      url: https://scheduler.example.test/zeitstempel
      navigation_goal: Waehle den aktuellen Monat aus und exportiere die Zeitstempel CSV fuer Team A und Team B.
      parameter_keys:
        - planning_credentials
    - block_type: navigation
      label: compare_on_call_matrix
      url: https://scheduler.example.test/matrix
      navigation_goal: Vergleiche Matrix und Rufbereitschaft mit der CSV und melde Zeitueberschneidungen.
      parameter_keys:
        - planning_credentials
"""
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="ASK_QUESTION",
        user_response="Welche URL des Planungs-Tools soll ich verwenden?",
        workflow_yaml=scheduling_yaml,
        has_workflow_proposal=True,
    )

    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes

    result = agent_module._build_output_policy_blocked_result(
        _ctx(),
        hard_block_output_policy_verdict(verdict),
        prior_global_llm_context="{}",
        prior_workflow_yaml="title: Prior",
    )

    assert result.response_type == "ASK_QUESTION"
    assert "credential" in result.user_response.lower()
    assert "confirm" in result.user_response.lower()
    assert "I could not safely return" not in result.user_response


def test_sdk_output_guardrail_ignores_internal_context_credential_ids() -> None:
    ctx = _ctx(
        request_policy=_policy(
            credential_input_kind="credential_name",
            credential_refs=["azure_credentials"],
            allow_run_blocks=False,
            allow_missing_credentials_in_draft=True,
        ),
    )
    ctx.last_workflow = object()
    ctx.last_workflow_yaml = """
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: azure_credentials
  blocks:
    - block_type: login
      label: login
      url: https://example.com/login
      parameter_keys:
        - azure_credentials
"""

    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        ctx,
        {
            "type": "REPLY",
            "user_response": "I have a draft workflow proposal. It has not been tested.",
            "global_llm_context": "Tool observation listed cred_unrelated for a different saved credential.",
        },
    )

    assert response_type == "REPLY"
    assert verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE not in verdict.reason_codes
    assert diagnostics["raw_would_have_failed"] is False
    assert diagnostics["contained_failure"] is False


def test_sdk_output_guardrail_defers_raw_question_after_untested_draft() -> None:
    ctx = _ctx(
        request_policy=_policy(
            testing_intent="skip_test",
            credential_input_kind="credential_name",
            credential_refs=["azure_credentials"],
            allow_run_blocks=False,
            allow_missing_credentials_in_draft=True,
            resolved_credentials=[],
        ),
    )
    ctx.allow_untested_workflow_draft = True
    ctx.last_workflow = object()
    ctx.last_update_block_count = 9
    ctx.last_workflow_yaml = """
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: azure_credentials
  blocks:
    - block_type: login
      label: login
      url: https://example.com/login
      parameter_keys:
        - azure_credentials
"""

    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        ctx,
        {
            "type": "ASK_QUESTION",
            "user_response": "I found cred_unrelated in an internal tool observation. Should I use it?",
        },
    )

    assert response_type == "ASK_QUESTION"
    assert verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE not in verdict.reason_codes
    assert diagnostics["raw_output_kind"] == "workflow_draft_proposal"
    assert diagnostics["final_output_kind"] == "workflow_draft_proposal"


def test_translation_surfaces_untested_draft_when_agent_asks_after_drafting() -> None:
    ctx = _ctx(
        request_policy=_policy(
            testing_intent="skip_test",
            credential_input_kind="credential_name",
            credential_refs=["azure_credentials"],
            allow_run_blocks=False,
            allow_missing_credentials_in_draft=True,
            resolved_credentials=[],
        ),
    )
    ctx.allow_untested_workflow_draft = True
    ctx.last_workflow = object()
    ctx.last_update_block_count = 9
    ctx.workflow_persisted = True
    ctx.last_workflow_yaml = """
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: azure_credentials
  blocks:
    - block_type: login
      label: login
      url: https://example.com/login
      parameter_keys:
        - azure_credentials
"""

    result = asyncio.run(
        agent_module._translate_to_agent_result(
            _fake_run_result(
                {
                    "type": "ASK_QUESTION",
                    "user_response": "I found cred_unrelated in an internal tool observation. Should I use it?",
                    "global_llm_context": "{}",
                }
            ),
            ctx,
            "{}",
            _chat_request(),
            "org-1",
        )
    )

    assert result.response_type == "REPLY"
    assert result.updated_workflow is ctx.last_workflow
    assert result.proposal_disposition == "review_untested"
    assert result.clear_proposed_workflow is False
    assert "without testing it" in result.user_response
    assert "not been verified" in result.user_response
    assert "cred_unrelated" not in result.user_response


@pytest.mark.asyncio
async def test_workflow_mutation_tools_have_sdk_input_guardrails_and_reject_raw_secret() -> None:
    from agents import ToolInputGuardrailData
    from agents.tool_context import ToolContext

    from skyvern.forge.sdk.copilot.tools import (
        _WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL,
        NATIVE_TOOLS,
    )

    guarded_tools = {
        tool.name: tool for tool in NATIVE_TOOLS if tool.name in {"update_workflow", "update_and_run_blocks"}
    }
    assert guarded_tools.keys() == {"update_workflow", "update_and_run_blocks"}
    assert guarded_tools["update_workflow"].tool_input_guardrails == [_WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL]
    assert guarded_tools["update_and_run_blocks"].tool_input_guardrails == [_WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL]

    result = await _WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL.run(
        ToolInputGuardrailData(
            context=ToolContext(
                context=_ctx(),
                tool_name="update_workflow",
                tool_call_id="call-1",
                tool_arguments=json.dumps(
                    {
                        "workflow_yaml": """
workflow_definition:
  blocks:
    - block_type: navigation
      label: login
      navigation_goal: Type password: hunter2 into the password field.
"""
                    }
                ),
            ),
            agent=SimpleNamespace(),
        )
    )

    assert result.behavior["type"] == "reject_content"
    assert "raw_secret_leak" in result.behavior["message"]


@pytest.mark.asyncio
async def test_update_and_run_blocks_rejects_unobserved_page_before_workflow_update(monkeypatch) -> None:
    from skyvern.forge.sdk.copilot import tools as tools_module

    workflow_yaml = """
workflow_definition:
  parameters: []
  blocks:
    - block_type: goto_url
      label: open_lookup
      url: https://example.com/lookup
    - block_type: navigation
      label: search_lookup
      navigation_goal: Enter the observed field and submit.
"""
    ctx = _ctx(
        build_phase=BuildPhase.COMPOSING,
        turn_intent=TurnIntent(mode=TurnIntentMode.BUILD),
    )

    async def unexpected_prior_definition(*args: object, **kwargs: object) -> object:
        raise AssertionError("composition evidence precheck must run before reading prior workflow")

    async def unexpected_update_workflow(*args: object, **kwargs: object) -> object:
        raise AssertionError("composition evidence precheck must run before updating workflow")

    sanitized_tool_names: list[str] = []

    def fake_sanitize_tool_result_for_llm(tool_name: str, result: dict[str, object]) -> dict[str, object]:
        sanitized_tool_names.append(tool_name)
        return result

    monkeypatch.setattr(tools_module, "_request_policy_allows_update_and_skip_run", lambda *args: False)
    monkeypatch.setattr(tools_module, "_authority_tool_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_tool_loop_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_get_prior_workflow_definition", unexpected_prior_definition)
    monkeypatch.setattr(tools_module, "_update_workflow", unexpected_update_workflow)
    monkeypatch.setattr(tools_module, "sanitize_tool_result_for_llm", fake_sanitize_tool_result_for_llm)

    result = await tools_module.update_and_run_blocks_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="update_and_run_blocks"),
        json.dumps(
            {
                "workflow_yaml": workflow_yaml,
                "block_labels": ["search_lookup"],
                "parameters": {},
            }
        ),
    )

    payload = json.loads(result)
    assert payload["ok"] is False
    assert "inspect_page_for_composition" in payload["error"]
    assert "https://example.com/lookup" in payload["error"]
    assert ctx.block_observation_refs == {}
    assert sanitized_tool_names == ["update_and_run_blocks"]


@pytest.mark.asyncio
async def test_update_workflow_runs_composition_evidence_precheck_before_saving(monkeypatch) -> None:
    from skyvern.forge.sdk.copilot import tools as tools_module

    workflow_yaml = """
workflow_definition:
  parameters: []
  blocks:
    - block_type: goto_url
      label: open_lookup
      url: https://example.com/lookup
    - block_type: navigation
      label: search_lookup
      navigation_goal: Enter the observed field and submit.
    - block_type: action
      label: expand_first_result
      navigation_goal: Expand the first result row.
"""
    ctx = _ctx(
        build_phase=BuildPhase.COMPOSING,
        turn_intent=TurnIntent(mode=TurnIntentMode.BUILD),
        request_policy=RequestPolicy(),
    )

    async def unexpected_update_workflow(*args: object, **kwargs: object) -> object:
        raise AssertionError("composition evidence precheck must run before update_workflow saves")

    monkeypatch.setattr(tools_module, "_tool_loop_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_update_workflow", unexpected_update_workflow)
    monkeypatch.setattr(tools_module, "_record_diagnosis_repair_contract", lambda *args, **kwargs: None)

    result = await tools_module.update_workflow_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="update_workflow"),
        json.dumps({"workflow_yaml": workflow_yaml}),
    )

    payload = json.loads(result)
    assert payload["ok"] is False
    assert "page-dependent build blocks need observed page evidence" in payload["error"]
    assert "https://example.com/lookup" in payload["error"]


@pytest.mark.asyncio
async def test_update_and_run_blocks_precheck_uses_proposed_block_observation_refs(monkeypatch) -> None:
    from skyvern.forge.sdk.copilot import tools as tools_module

    workflow_yaml = """
workflow_definition:
  parameters: []
  blocks:
    - block_type: goto_url
      label: open_home
      url: https://example.com/
    - block_type: action
      label: open_results
      navigation_goal: Open the observed result list.
    - block_type: extraction
      label: read_results
      data_extraction_goal: Read the visible result rows.
"""
    ctx = _ctx(
        build_phase=BuildPhase.COMPOSING,
        turn_intent=TurnIntent(mode=TurnIntentMode.BUILD),
        flow_evidence=[
            {
                "step": 0,
                "reached_via": "navigate",
                "url": "https://example.com/",
                "had_bounded_schema": True,
                "evidence": {
                    "source_tool": "inspect_page_for_composition",
                    "inspected_url": "https://example.com/",
                    "current_url": "https://example.com/",
                    "forms": [{"fields": [{"name": "q", "selector": "#q"}], "submit_controls": []}],
                    "navigation_targets": [],
                    "result_containers": [],
                    "challenge_controls": [],
                },
            },
            {
                "step": 1,
                "reached_via": "interaction",
                "url": "https://example.com/results",
                "had_bounded_schema": True,
                "evidence": {
                    "source_tool": "inspect_page_for_composition",
                    "inspected_url": "https://example.com/results",
                    "current_url": "https://example.com/results",
                    "forms": [],
                    "navigation_targets": [],
                    "result_containers": [{"selector": "#results"}],
                    "challenge_controls": [],
                },
            },
        ],
    )

    captured: dict[str, object] = {}

    async def fake_update_workflow(payload: dict, update_ctx: CopilotContext, **kwargs: object) -> dict:
        captured["payload"] = payload
        workflow = SimpleNamespace(workflow_definition={"blocks": [{"label": "open_results"}]})
        update_ctx.last_workflow = workflow
        update_ctx.last_update_block_count = 2
        return {"ok": True, "_workflow": workflow, "data": {"block_count": 2}}

    async def fake_run_blocks(params: dict, run_ctx: CopilotContext, **kwargs: object) -> dict:
        return {
            "ok": True,
            "data": {
                "workflow_run_id": "wr-1",
                "overall_status": "completed",
                "blocks": [],
            },
        }

    async def fake_prior_definition(update_ctx: CopilotContext) -> object:
        return None

    monkeypatch.setattr(tools_module, "_request_policy_allows_update_and_skip_run", lambda *args: False)
    monkeypatch.setattr(tools_module, "_authority_tool_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_tool_loop_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "_get_prior_workflow_definition", fake_prior_definition)
    monkeypatch.setattr(tools_module, "_update_workflow", fake_update_workflow)
    monkeypatch.setattr(tools_module, "_pre_run_workflow_coverage_error", lambda *args: None)
    monkeypatch.setattr(tools_module, "_plan_frontier", lambda *args: (["open_results"], {}, "open_results"))
    monkeypatch.setattr(tools_module, "_frontier_run_size_error", lambda *args: None)
    monkeypatch.setattr(tools_module, "_run_blocks_and_collect_debug", fake_run_blocks)
    monkeypatch.setattr(tools_module, "_record_diagnosis_repair_contract", lambda *args, **kwargs: None)
    monkeypatch.setattr(tools_module, "enqueue_screenshot_from_result", lambda *args, **kwargs: None)

    result = await tools_module.update_and_run_blocks_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="update_and_run_blocks"),
        json.dumps(
            {
                "workflow_yaml": workflow_yaml,
                "block_labels": ["open_results", "read_results"],
                "block_observation_refs": [
                    {"label": "open_results", "observation_step": 0},
                    {"label": "read_results", "observation_step": 1},
                ],
                "code_artifact_metadata": _code_artifact_metadata(),
                "parameters": {},
            }
        ),
    )

    assert json.loads(result)["ok"] is True
    assert captured["payload"]["workflow_yaml"] == workflow_yaml
    assert captured["payload"]["block_observation_refs"] == {
        "open_results": 0,
        "read_results": 1,
    }
    assert [ref.model_dump() for ref in captured["payload"]["raw_block_observation_refs"]] == [
        {"label": "open_results", "observation_step": 0},
        {"label": "read_results", "observation_step": 1},
    ]
    assert captured["payload"]["code_artifact_metadata"][0]["block_label"] == "open_results"
    assert captured["payload"]["code_artifact_metadata"][0]["claimed_outcomes"][0]["id"] == "claim:open_results"
    assert (
        captured["payload"]["code_artifact_metadata"][0]["evidence_refs"][0]["evidence_ref"] == "evidence:result_link"
    )
    assert [
        item.model_dump(mode="json", exclude_none=True) for item in captured["payload"]["raw_code_artifact_metadata"]
    ] == captured["payload"]["code_artifact_metadata"]
    assert ctx.block_observation_refs == {}


@pytest.mark.asyncio
async def test_update_workflow_rejects_raw_secret_before_processing(monkeypatch) -> None:
    from skyvern.forge.sdk.copilot.tools import _update_workflow

    process_mock = MagicMock()
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.workflow_update._process_workflow_yaml", process_mock)

    result = await _update_workflow(
        {
            "workflow_yaml": """
workflow_definition:
  blocks:
    - block_type: navigation
      label: login
      navigation_goal: Type password: hunter2 into the password field.
"""
        },
        _ctx(),
        allow_missing_credentials=True,
    )

    assert result["ok"] is False
    assert "raw_secret_leak" in result["error"]
    process_mock.assert_not_called()


def test_inline_replace_workflow_rejects_raw_secret_before_processing(monkeypatch) -> None:
    process_mock = MagicMock()
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.workflow_update._process_workflow_yaml", process_mock)
    result = _fake_run_result(
        {
            "type": "REPLACE_WORKFLOW",
            "user_response": "Here is the workflow.",
            "workflow_yaml": """
workflow_definition:
  blocks:
    - block_type: navigation
      label: login
      navigation_goal: Type password: hunter2 into the password field.
""",
        }
    )

    agent_result = asyncio.run(
        agent_module._translate_to_agent_result(
            result,
            _ctx(),
            global_llm_context=None,
            chat_request=_chat_request(),
            organization_id="org-1",
        )
    )

    assert agent_result.response_type == "ASK_QUESTION"
    assert agent_result.updated_workflow is None
    assert agent_result.clear_proposed_workflow is False
    assert agent_result.proposal_disposition == "no_proposal"
    process_mock.assert_not_called()


def test_translate_to_agent_result_blocks_raw_secret_final_text() -> None:
    result = _fake_run_result({"type": "REPLY", "user_response": "I used password: hunter2."})

    agent_result = asyncio.run(
        agent_module._translate_to_agent_result(
            result,
            _ctx(),
            global_llm_context=None,
            chat_request=_chat_request(),
            organization_id="org-1",
        )
    )

    assert agent_result.response_type == "ASK_QUESTION"
    assert agent_result.updated_workflow is None
    assert agent_result.clear_proposed_workflow is False
    assert agent_result.proposal_disposition == "no_proposal"
    assert "hunter2" not in agent_result.user_response
    assert "DO NOT PROVIDE RAW LOGIN/PASSWORD" in agent_result.user_response


def test_output_policy_blocks_late_block_running_instruction_leak() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=(
            "Less than 90 seconds remain in this Copilot turn after the previous workflow run failed. "
            "Do NOT retry block-running tools."
        ),
    )

    assert OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK in verdict.reason_codes


def test_translate_scrubs_late_block_running_leak_and_preserves_draft() -> None:
    ctx = _ctx()
    saved_workflow = object()
    ctx.last_workflow = saved_workflow
    ctx.last_workflow_yaml = "workflow_definition:\n  blocks: []\n"
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text="Stop tool use and answer from gathered evidence.",
        user_facing_reason="I'm running out of time on this turn. I'll wrap up with what I have so far.",
        recovery_hint="stop",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=False,
        internal_reason_code="tool_error_late_block_running",
        blocked_tool="update_and_run_blocks",
    )

    agent_result = asyncio.run(
        agent_module._translate_to_agent_result(
            _fake_run_result(
                {
                    "type": "REPLY",
                    "user_response": (
                        "Less than 90 seconds remain in this Copilot turn after the previous workflow run failed. "
                        "Do NOT retry block-running tools."
                    ),
                }
            ),
            ctx,
            global_llm_context=None,
            chat_request=_chat_request(),
            organization_id="org-1",
        )
    )

    assert agent_result.updated_workflow is saved_workflow
    assert agent_result.clear_proposed_workflow is False
    assert agent_result.proposal_disposition == "review_untested"
    assert "Do NOT retry" not in agent_result.user_response
    assert "workflow draft is still saved" in agent_result.user_response


def test_timeout_exit_scrubs_recorded_late_block_running_leak_and_preserves_draft() -> None:
    ctx = _ctx()
    saved_workflow = object()
    ctx.last_workflow = saved_workflow
    ctx.last_workflow_yaml = "workflow_definition:\n  blocks: []\n"
    ctx.last_update_block_count = 5
    ctx.last_test_ok = False
    ctx.last_test_failure_reason = (
        "Less than 90 seconds remain in this Copilot turn after the previous workflow run failed. "
        "Do NOT retry block-running tools."
    )

    agent_result = agent_module._build_timeout_exit_result(ctx, global_llm_context=None)

    assert agent_result.updated_workflow is saved_workflow
    assert agent_result.clear_proposed_workflow is False
    assert agent_result.proposal_disposition == "review_untested"
    assert "Do NOT retry" not in agent_result.user_response
    assert "draft workflow proposal" in agent_result.user_response


def test_translate_to_agent_result_rewrites_unbacked_workflow_claim() -> None:
    result = _fake_run_result({"type": "REPLY", "user_response": "I've drafted a workflow for you."})

    agent_result = asyncio.run(
        agent_module._translate_to_agent_result(
            result,
            _ctx(),
            global_llm_context=None,
            chat_request=_chat_request(),
            organization_id="org-1",
        )
    )

    assert "wasn't able to produce a workflow proposal" in agent_result.user_response
    assert "provide the missing details" not in agent_result.user_response
    assert "couldn't identify which details were missing" in agent_result.user_response
    assert agent_result.response_type == "ASK_QUESTION"
    assert agent_result.updated_workflow is None
    assert agent_result.output_policy_diagnostics == {
        "raw_output_kind": "informational_answer",
        "final_output_kind": "clarification_request",
        "raw_reason_codes": ["unbacked_workflow_delivery_claim", "missing_proposal_state"],
        "hard_block_reason_codes": [],
        "soft_rewrite_reason_codes": ["unbacked_workflow_delivery_claim", "missing_proposal_state"],
        "raw_would_have_failed": True,
        "contained_failure": True,
        "final_output_policy_allowed": True,
    }


def test_translate_to_agent_result_rewrites_deprecated_block_taxonomy() -> None:
    result = _fake_run_result(
        {
            "type": "REPLY",
            "user_response": (
                "`task_v2` is deprecated. Use these newer block types instead: "
                "`navigation`, `extraction`, `validation`, `login`, `goto_url`, and `file_download`."
            ),
        }
    )

    with patch("skyvern.forge.sdk.copilot.agent.LOG.info") as log_info:
        agent_result = asyncio.run(
            agent_module._translate_to_agent_result(
                result,
                _ctx(),
                global_llm_context=None,
                chat_request=_chat_request(),
                organization_id="org-1",
            )
        )

    assert "task_v2" not in agent_result.user_response
    assert "`navigation`" not in agent_result.user_response
    assert "`extraction`" not in agent_result.user_response
    assert "Describe the page action" in agent_result.user_response
    assert agent_result.response_type == "REPLY"
    assert agent_result.updated_workflow is None
    final_log = next(call for call in log_info.call_args_list if call.args[0] == "copilot output policy final verdict")
    assert final_log.kwargs["allowed"] is True
    assert final_log.kwargs["reason_codes"] == []
    assert final_log.kwargs["raw_output_kind"] == "informational_answer"
    assert final_log.kwargs["final_output_kind"] == "informational_answer"
    assert final_log.kwargs["hard_block_reason_codes"] == []
    assert final_log.kwargs["soft_rewrite_reason_codes"] == ["internal_block_taxonomy_leak"]
    assert final_log.kwargs["raw_would_have_failed"] is True
    assert final_log.kwargs["contained_failure"] is True
    assert agent_result.output_policy_diagnostics == {
        "raw_output_kind": "informational_answer",
        "final_output_kind": "informational_answer",
        "raw_reason_codes": ["internal_block_taxonomy_leak"],
        "hard_block_reason_codes": [],
        "soft_rewrite_reason_codes": ["internal_block_taxonomy_leak"],
        "raw_would_have_failed": True,
        "contained_failure": True,
        "final_output_policy_allowed": True,
    }


def test_translate_to_agent_result_rewrites_internal_classifier_vocab_leak() -> None:
    leak = (
        "TurnIntent classified this turn as `edit`, so the request couldn't continue. "
        "safe_reason_code=turn_intent_no_mutation_run_blocked."
    )
    result = _fake_run_result({"type": "REPLY", "user_response": leak})

    agent_result = asyncio.run(
        agent_module._translate_to_agent_result(
            result,
            _ctx(),
            global_llm_context=None,
            chat_request=_chat_request(),
            organization_id="org-1",
        )
    )

    assert "TurnIntent" not in agent_result.user_response
    assert "safe_reason_code" not in agent_result.user_response
    assert "Tell me what you'd like to do next" in agent_result.user_response
    diagnostics = agent_result.output_policy_diagnostics or {}
    assert "internal_classifier_vocab_leak" in diagnostics["soft_rewrite_reason_codes"]
    assert "internal_classifier_vocab_leak" in diagnostics["raw_reason_codes"]
    assert diagnostics["raw_would_have_failed"] is True
    assert diagnostics["contained_failure"] is True


def test_translate_to_agent_result_hard_blocks_extended_taxonomy_tool_name_leak() -> None:
    leak = "Trace shows `get_run_results` was the last tool dispatched on this turn."
    result = _fake_run_result({"type": "REPLY", "user_response": leak})

    agent_result = asyncio.run(
        agent_module._translate_to_agent_result(
            result,
            _ctx(),
            global_llm_context=None,
            chat_request=_chat_request(),
            organization_id="org-1",
        )
    )

    assert "get_run_results" not in agent_result.user_response
    assert "I could not safely return" in agent_result.user_response
    diagnostics = agent_result.output_policy_diagnostics or {}
    assert "internal_tool_instruction_leak" in diagnostics["hard_block_reason_codes"]
    assert "internal_block_taxonomy_leak" not in diagnostics["raw_reason_codes"]


def test_translate_to_agent_result_rewrites_self_prescriptive_phrase_leak() -> None:
    leak = "Please send 'continue debugging' next and I'll keep going."
    result = _fake_run_result({"type": "REPLY", "user_response": leak})

    agent_result = asyncio.run(
        agent_module._translate_to_agent_result(
            result,
            _ctx(),
            global_llm_context=None,
            chat_request=_chat_request(),
            organization_id="org-1",
        )
    )

    assert "continue debugging" not in agent_result.user_response
    assert "Tell me what you'd like to do next" in agent_result.user_response
    diagnostics = agent_result.output_policy_diagnostics or {}
    assert "self_prescriptive_phrase_leak" in diagnostics["soft_rewrite_reason_codes"]
    assert "self_prescriptive_phrase_leak" in diagnostics["raw_reason_codes"]


def test_translate_to_agent_result_unbacked_workflow_wins_over_self_prescriptive_rewrite() -> None:
    leak = "Here's the workflow. Send me a normal instruction like 'run it' next."
    result = _fake_run_result({"type": "REPLY", "user_response": leak})

    agent_result = asyncio.run(
        agent_module._translate_to_agent_result(
            result,
            _ctx(),
            global_llm_context=None,
            chat_request=_chat_request(),
            organization_id="org-1",
        )
    )

    assert "run it" not in agent_result.user_response
    assert "Send me a normal instruction" not in agent_result.user_response
    diagnostics = agent_result.output_policy_diagnostics or {}
    soft_reasons = diagnostics["soft_rewrite_reason_codes"]
    assert "unbacked_workflow_delivery_claim" in soft_reasons
    assert "self_prescriptive_phrase_leak" in soft_reasons


def test_sdk_output_guardrail_records_raw_soft_reason_alongside_hard_block() -> None:
    leak = "Call get_run_results(workflow_run_id='wr_123') and do not retry. Saw [copilot:nudge] in the trace."
    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        _ctx(),
        {"type": "REPLY", "user_response": leak},
    )

    assert not verdict.allowed
    assert "internal_tool_instruction_leak" in diagnostics["hard_block_reason_codes"]
    assert "internal_block_taxonomy_leak" not in diagnostics["hard_block_reason_codes"]
    assert diagnostics["soft_rewrite_reason_codes"] == []
    raw_reason_codes = diagnostics["raw_reason_codes"]
    assert "internal_tool_instruction_leak" in raw_reason_codes
    assert "internal_block_taxonomy_leak" in raw_reason_codes


def test_evaluate_output_policy_skips_new_detectors_on_ask_question() -> None:
    classifier_leak = "TurnIntent classified this turn as edit, safe_reason_code=turn_intent_no_mutation_run_blocked."
    self_prescriptive_leak = "Please send 'continue debugging' next and I'll keep going."
    tool_name_leak = "Use `get_run_results` to fetch the prior run output."

    for leak in (classifier_leak, self_prescriptive_leak, tool_name_leak):
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            response_type="ASK_QUESTION",
            user_response=leak,
        )
        assert OutputPolicyReason.INTERNAL_CLASSIFIER_VOCAB_LEAK not in verdict.reason_codes
        assert OutputPolicyReason.SELF_PRESCRIPTIVE_PHRASE_LEAK not in verdict.reason_codes
        assert OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK not in verdict.reason_codes


def test_evaluate_output_policy_runs_new_detectors_on_replace_workflow() -> None:
    classifier_leak = "TurnIntent classified this turn as edit, safe_reason_code=turn_intent_no_mutation_run_blocked."
    self_prescriptive_leak = "Please send 'continue debugging' next and I'll keep going."
    tool_name_leak = "Use `get_run_results` to fetch the prior run output."

    classifier_verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLACE_WORKFLOW",
        user_response=classifier_leak,
    )
    assert OutputPolicyReason.INTERNAL_CLASSIFIER_VOCAB_LEAK in classifier_verdict.reason_codes

    self_prescriptive_verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLACE_WORKFLOW",
        user_response=self_prescriptive_leak,
    )
    assert OutputPolicyReason.SELF_PRESCRIPTIVE_PHRASE_LEAK in self_prescriptive_verdict.reason_codes

    tool_name_verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLACE_WORKFLOW",
        user_response=tool_name_leak,
    )
    assert OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK in tool_name_verdict.reason_codes


@pytest.mark.parametrize(
    "user_response,expected",
    [
        ("LOOP DETECTED: same tool dispatched three times in a row.", True),
        ("Saw [copilot:nudge] in the trace before the failure.", True),
        ("Couldn't finish the diagnostic step on this nudge turn.", True),
        ("nudge-turn was cleared before the next iteration.", True),
        ("Update the workflow to log in before extracting data.", False),
        ("I'll get the run results once it finishes.", False),
        ("", False),
    ],
)
def test_contains_internal_tool_vocab_leak_helper(user_response: str, expected: bool) -> None:
    assert _contains_internal_tool_vocab_leak(user_response) is expected


def test_translate_to_agent_result_prioritizes_unbacked_workflow_claim_over_taxonomy_rewrite() -> None:
    result = _fake_run_result(
        {
            "type": "REPLY",
            "user_response": "I've drafted a workflow for you using `task_v2`.",
        }
    )

    agent_result = asyncio.run(
        agent_module._translate_to_agent_result(
            result,
            _ctx(),
            global_llm_context=None,
            chat_request=_chat_request(),
            organization_id="org-1",
        )
    )

    assert "wasn't able to produce a workflow proposal" in agent_result.user_response
    assert "task_v2" not in agent_result.user_response
    assert agent_result.output_policy_diagnostics is not None
    assert "unbacked_workflow_delivery_claim" in agent_result.output_policy_diagnostics["soft_rewrite_reason_codes"]
    assert agent_result.updated_workflow is None


def test_translate_to_agent_result_rewrites_block_yaml_pasted_in_reply() -> None:
    leak = (
        "I've now updated the workflow to also accept the form's URL as a parameter, named `form_url`. "
        "Here's how the block now looks:\n\n"
        "    - label: navigate_and_fill_form\n"
        "      block_type: navigation\n"
        "      navigation_goal: Fill the abuse form.\n"
        '      url: "{{ form_url }}"\n'
        "      parameter_keys:\n"
        "        - name\n"
        "        - form_url\n"
    )
    result = _fake_run_result({"type": "REPLY", "user_response": leak})

    with patch("skyvern.forge.sdk.copilot.agent.LOG.info") as log_info:
        agent_result = asyncio.run(
            agent_module._translate_to_agent_result(
                result,
                _ctx(),
                global_llm_context=None,
                chat_request=_chat_request(),
                organization_id="org-1",
            )
        )

    assert "block_type" not in agent_result.user_response
    assert "navigation_goal" not in agent_result.user_response
    assert "parameter_keys" not in agent_result.user_response
    assert "haven't applied it yet" in agent_result.user_response
    assert agent_result.updated_workflow is None
    final_log = next(call for call in log_info.call_args_list if call.args[0] == "copilot output policy final verdict")
    assert final_log.kwargs["soft_rewrite_reason_codes"] == ["workflow_yaml_in_reply"]
    assert final_log.kwargs["contained_failure"] is True
    assert agent_result.output_policy_diagnostics["soft_rewrite_reason_codes"] == ["workflow_yaml_in_reply"]
    assert agent_result.output_policy_diagnostics["final_output_policy_allowed"] is True


def test_translate_to_agent_result_rewrites_block_yaml_when_workflow_attached() -> None:
    leak = (
        "I've now updated the workflow to also accept the form's URL as a parameter, named `form_url`. "
        "Here's how the block now looks:\n\n"
        "    - label: navigate_and_fill_form\n"
        "      block_type: navigation\n"
        "      navigation_goal: Fill the abuse form.\n"
        "      parameter_keys:\n"
        "        - form_url\n"
    )
    result = _fake_run_result({"type": "REPLY", "user_response": leak})

    workflow = SimpleNamespace(name="draft")
    ctx = _ctx()
    ctx.last_workflow = workflow
    ctx.last_workflow_yaml = _workflow_yaml()
    ctx.last_test_ok = True

    agent_result = asyncio.run(
        agent_module._translate_to_agent_result(
            result,
            ctx,
            global_llm_context=None,
            chat_request=_chat_request(),
            organization_id="org-1",
        )
    )

    assert "block_type" not in agent_result.user_response
    assert "navigation_goal" not in agent_result.user_response
    assert "parameter_keys" not in agent_result.user_response
    assert "haven't applied it yet" not in agent_result.user_response
    assert "made the change" in agent_result.user_response
    assert agent_result.updated_workflow is workflow


def test_translate_to_agent_result_adds_unvalidated_affordance() -> None:
    workflow = SimpleNamespace(name="draft")
    result = _fake_run_result({"type": "REPLY", "user_response": "I drafted the workflow."})

    agent_result = asyncio.run(
        agent_module._translate_to_agent_result(
            result,
            _ctx(last_workflow=workflow, last_workflow_yaml="title: draft", last_test_ok=None),
            global_llm_context=None,
            chat_request=_chat_request(),
            organization_id="org-1",
        )
    )

    assert agent_result.updated_workflow is workflow
    assert "Accept to save" in agent_result.user_response
    assert "Reject to discard" in agent_result.user_response


def _two_credential_workflow_yaml() -> str:
    return """
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: site_a_credentials
      default_value: cred_amazon
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: site_b_credentials
      default_value: cred_quicken
  blocks:
    - block_type: login
      label: login_a
      url: https://login-a.authenticationtest.test/login
      navigation_goal: Log in to site A.
      parameter_keys:
        - site_a_credentials
    - block_type: login
      label: login_b
      url: https://login-b.authenticationtest.test/login
      navigation_goal: Log in to site B.
      parameter_keys:
        - site_b_credentials
"""


def _discovered(credential_id: str, tested_url: str | None) -> object:
    return SimpleNamespace(credential_id=credential_id, name=credential_id, tested_url=tested_url)


def test_allows_discovered_bound_credential_with_no_resolved_credentials() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            discovered_credentials=[_discovered("cred_safe", "https://login.example.test/login")],
            credential_input_kind="none",
        ),
        workflow_yaml=_workflow_yaml(navigation_goal="Log in."),
    )

    assert verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE not in verdict.reason_codes


def test_allows_two_discovered_bound_credentials_in_one_save() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            discovered_credentials=[
                _discovered("cred_amazon", "https://login-a.authenticationtest.test/login"),
                _discovered("cred_quicken", "https://login-b.authenticationtest.test/login"),
            ],
            credential_input_kind="none",
        ),
        workflow_yaml=_two_credential_workflow_yaml(),
    )

    assert verdict.allowed


def test_allows_resolved_and_discovered_credentials_together_in_one_save() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[_credential("cred_amazon", "https://login-a.authenticationtest.test/login")],
            discovered_credentials=[_discovered("cred_quicken", "https://login-b.authenticationtest.test/login")],
            credential_input_kind="none",
        ),
        workflow_yaml=_two_credential_workflow_yaml(),
    )

    assert verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE not in verdict.reason_codes
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE not in verdict.reason_codes


def test_rejects_when_only_one_of_two_bound_credentials_is_discovered() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            discovered_credentials=[_discovered("cred_amazon", "https://login-a.authenticationtest.test/login")],
            credential_input_kind="none",
        ),
        workflow_yaml=_two_credential_workflow_yaml(),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes


def test_rejects_discovered_credential_id_that_is_not_bound_in_workflow() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            discovered_credentials=[_discovered("cred_safe", "https://login.example.test/login")],
            credential_input_kind="none",
        ),
        user_response="I used cred_safe for the login.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes


def test_rejects_fabricated_credential_id_not_in_discovered_set() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            discovered_credentials=[_discovered("cred_amazon", "https://login.example.test/login")],
            credential_input_kind="none",
        ),
        workflow_yaml=_workflow_yaml(navigation_goal="Log in."),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes


def test_rejects_discovered_credential_bound_to_new_origin() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(
            resolved_credentials=[],
            discovered_credentials=[_discovered("cred_safe", "https://login.example.test/login")],
            credential_input_kind="none",
        ),
        workflow_yaml=_workflow_yaml(url="https://evil.example.test/login"),
    )

    assert not verdict.allowed
    assert OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED in verdict.reason_codes

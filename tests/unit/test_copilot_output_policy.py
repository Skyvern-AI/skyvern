from __future__ import annotations

import asyncio
import base64
import json
import string
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from agents import GuardrailFunctionOutput, OutputGuardrail
from agents.run_context import RunContextWrapper

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot import output_policy as output_policy_module
from skyvern.forge.sdk.copilot.author_time_block import (
    AUTHOR_TIME_HARD_BLOCKS,
    BANNED_BLOCKS_BLOCK_ID,
    CODE_SAFETY_BLOCK_ID,
    CREDENTIAL_SCOUT_BLOCK_ID,
    AuthorTimeBlock,
)
from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
    contains_internal_machinery_leak,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.output_policy import (
    CopilotOutputKind,
    OutputPolicyReason,
    OutputPolicyVerdict,
    derive_output_kind,
    evaluate_output_policy,
    hard_block_output_policy_verdict,
    normalize_response_scaffolding,
    raw_secret_label,
)
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.review_gate import workflow_block_fingerprints
from skyvern.forge.sdk.copilot.tools.run_execution import _watchdog_error_message
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome

# Assembled at runtime so the source never contains a token-shaped literal for secret scanners to flag.
_FAKE_OPENAI_KEY = "sk-" + string.ascii_lowercase[:24]
_FAKE_JWT = ".".join(
    (
        *(
            base64.urlsafe_b64encode(json.dumps(part, separators=(",", ":")).encode()).decode().rstrip("=")
            for part in ({"alg": "HS256"}, {"sub": "1234567890"})
        ),
        "x" * 20,
    )
)


def _credential(
    credential_id: str = "cred_safe", tested_url: str | None = "https://login.example.test/login"
) -> SimpleNamespace:
    return SimpleNamespace(credential_id=credential_id, name="Saved Login", tested_url=tested_url, extra_site_urls=[])


def _policy(**overrides: object) -> RequestPolicy:
    defaults = dict(resolved_credentials=[_credential()])
    defaults.update(overrides)
    return RequestPolicy(**defaults)


_COVERED_DRAFT_YAML = """title: Draft\nworkflow_definition:\n  parameters: []\n  blocks:\n  - block_type: task\n    label: step\n    prompt: Do it\n"""


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


def _live_fill_policy() -> RequestPolicy:
    return _policy(
        authoring_intent="defer_authoring",
        allow_update_workflow=False,
        allow_run_blocks=False,
        completion_criteria=[
            CompletionCriterion(
                id="c0",
                outcome="The form is completed.",
                kind="terminal_action",
                terminal_action_family="form",
            )
        ],
    )


def _defer_authoring_policy() -> RequestPolicy:
    return _policy(
        authoring_intent="defer_authoring",
        allow_update_workflow=False,
        allow_run_blocks=False,
    )


def _fake_run_result(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(final_output=json.dumps(payload), new_items=[])


def _chat_request() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_copilot_chat_id="chat-1",
        workflow_yaml="title: Prior",
        product_action=None,
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
        pytest.param(
            "Next step: call get_run_results with this workflow_run_id.",
            id="call-get-run-results-instruction",
        ),
        pytest.param(
            'Call `get_run_results(workflow_run_id="wr_123")` first, then await user input.',
            id="call-get-run-results-backtick-await",
        ),
        pytest.param(
            "Then call update_and_run_blocks with a smaller chain.",
            id="call-update-and-run-blocks-instruction",
        ),
        pytest.param(
            'I\'ll call get_run_results(workflow_run_id="wr_123") and report back.',
            id="call-get-run-results-report-back",
        ),
        pytest.param(
            "Next I need to run_blocks_and_collect_debug[block_labels=['login']].",
            id="run-blocks-and-collect-debug",
        ),
        pytest.param(
            "Use `update_workflow` to apply that change.",
            id="backtick-update-workflow-tool-name",
        ),
        pytest.param(
            "Trace shows get_run_results was the last tool dispatched on this turn.",
            id="trace-mentions-get-run-results",
        ),
        pytest.param(
            "Falling back to list_credentials since the user did not specify one.",
            id="list-credentials-fallback",
        ),
    ],
)
def test_machine_format_machinery_leak_predicate_matches(user_response: str) -> None:
    assert contains_internal_machinery_leak(user_response) is True


def test_watchdog_model_facing_text_is_allowed_as_a_run_result_reply() -> None:
    verdict = evaluate_output_policy(
        request_policy=None,
        response_type="REPLY",
        user_response=asyncio.run(
            _watchdog_error_message("ceiling", SimpleNamespace(), "wr_123", None, 300, dispatch_to_worker=True)
        ),
        output_kind=CopilotOutputKind.WORKFLOW_RUN_RESULT,
    )

    assert verdict.allowed
    assert verdict.reason_codes == []


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


def test_rejects_raw_secret_in_the_reply() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response="Log in with password: hunter2.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes


class TestSanctionedSecretReferenceIdiom:
    """`password = parameters[...]` / attribute-chain reads are references to bound
    values, not leaks — quoting the code-only authoring idiom back to the user must pass the
    syntactic backstop while literal values keep blocking."""

    def test_allows_parameters_subscript_assignment_in_tool_arguments(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=('code: |\n  password = parameters["login_credential"]'),
        )

        assert verdict.allowed

    def test_allows_credential_attribute_read_in_workflow_yaml(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=(
                "code: |\n"
                "  username = login_credential.username\n"
                "  password = login_credential.password\n"
                '  await page.locator("#passwordInput").fill(login_credential.password)\n'
            ),
        )

        assert verdict.allowed

    def test_allows_awaited_credential_otp_assignment_in_workflow_yaml(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=(
                'code: |\n  passcode = await login_credentials.otp()\n  await page.locator("#code").fill(passcode)\n'
            ),
        )

        assert verdict.allowed

    def test_allows_keyword_secret_reference_to_awaited_credential_otp_in_tool_arguments(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=("code: |\n  token = await login_credential.otp()"),
        )

        assert verdict.allowed

    def test_rejects_literal_secret_even_when_same_line_has_credential_reference(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response="code: |\n  password = 'hunter2'; password_ref = login_credential.password",
        )

        assert not verdict.allowed
        assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_allows_str_wrapped_parameter_reference(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=('code: |\n  token = str(parameters.get("api_token"))'),
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
                user_response=(f"code: |\n  password = {rhs}"),
            )

            assert not verdict.allowed, rhs
            assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_still_rejects_awaited_non_credential_secret_source(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=("code: |\n  passcode = await page.locator('#code').inner_text()"),
        )

        assert not verdict.allowed
        assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_allows_keyword_argument_reference_with_closing_paren(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=('code: |\n  await do_login(password=parameters["cred"])'),
        )

        assert verdict.allowed

    def test_still_rejects_jwt_shaped_literal_assignment(self) -> None:
        # A dotted literal (JWT-shaped) must not pass as an "attribute chain".
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=("code: |\n  token = eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig"),
        )

        assert not verdict.allowed
        assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_still_rejects_dotted_literal_not_ending_in_credential_field(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=("code: |\n  password = admin.password123"),
        )

        assert not verdict.allowed
        assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_still_rejects_quoted_literal_assignment(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=('code: |\n  password = "hunter2"'),
        )

        assert not verdict.allowed
        assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_still_rejects_bare_literal_assignment(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=("password: hunter2"),
        )

        assert not verdict.allowed
        assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_allows_lexical_token_parsed_out_of_page_text(self) -> None:
        for rhs in (
            "parts[0]",
            "text.split(' logs found')[0]",
            "m.group(1)",
        ):
            verdict = evaluate_output_policy(
                request_policy=_policy(),
                user_response=(f"code: |\n  first_token = {rhs}"),
            )

            assert verdict.allowed, rhs

    def test_verdict_does_not_depend_on_how_the_yaml_escapes_the_code(self) -> None:
        block_scalar = 'code: |\n  _entry = page.locator("#password")\n  count_token = evidence_text.split(" ")[0]\n'
        escaped_scalar = (
            'code: "_entry = page.locator(\\"#password\\")\\ncount_token = evidence_text.split(\\" \\")[0]"'
        )

        for workflow_yaml in (block_scalar, escaped_scalar):
            verdict = evaluate_output_policy(
                request_policy=_policy(),
                user_response=(workflow_yaml),
            )

            assert verdict.allowed, workflow_yaml

    def test_allows_a_newline_literal_the_block_scalar_keeps_as_code_data(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=('code: |\n  count_token = text.split("\\n")[0]\n'),
        )

        assert verdict.allowed

    def test_a_value_shaped_secret_inside_a_call_argument_still_blocks(self) -> None:
        # The lexical-token exemption clears only the keyword-assignment match; the value-shaped
        # patterns scan the whole text independently, so hiding a real secret as a call argument
        # does not ride out on the exemption.
        for rhs in (
            f'cache.get("{_FAKE_OPENAI_KEY}")',
            f'store.lookup("{_FAKE_JWT}")',
        ):
            verdict = evaluate_output_policy(
                request_policy=_policy(),
                user_response=f"code: |\n  token = {rhs}",
            )

            assert not verdict.allowed, rhs
            assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_still_rejects_literal_secret_under_a_lexical_token_name(self) -> None:
        # The literal deliberately contains no secret keyword, so only the token arm can reject it.
        for lhs in ("first_token", "result_token", "token"):
            verdict = evaluate_output_policy(
                request_policy=_policy(),
                user_response=(f"code: |\n  {lhs} = 'ghp_abcdefghijklmnopqrstuvwxyz'"),
            )

            assert not verdict.allowed, lhs
            assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_still_rejects_a_literal_secret_wrapped_to_look_parsed(self) -> None:
        for rhs in (
            'identity("ghp_abcdefghijklmnopqrstuvwxyz")',
            '("ghp_abcdefghijklmnopqrstuvwxyz", parts)[0]',
            '["ghp_abcdefghijklmnopqrstuvwxyz"][0]',
            '"ghp_abcdefghijklmnopqrstuvwxyz".strip()',
            'str("ghp_abcdefghijklmnopqrstuvwxyz")',
        ):
            verdict = evaluate_output_policy(
                request_policy=_policy(),
                user_response=(f"code: |\n  first_token = {rhs}"),
            )

            assert not verdict.allowed, rhs
            assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_still_rejects_page_read_into_an_unambiguous_secret_name(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=("code: |\n  password_token = await page.locator('#p').inner_text()"),
        )

        assert not verdict.allowed
        assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_still_rejects_email_password_pair_next_to_reference_idiom(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=_policy(),
            user_response=('code: |\n  password = parameters["cred"]\nnotes: qa.user@example.test:FakePass123!'),
        )

        assert not verdict.allowed
        assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes

    def test_chat_surface_request_policy_detection_is_unchanged(self) -> None:
        # The pre-agent chat-surface detector stays conservative: even the code
        # idiom pasted into chat still counts as a raw-secret signal there.
        from skyvern.forge.sdk.copilot.request_policy import _raw_secret_detected

        assert _raw_secret_detected("password: hunter2")
        assert _raw_secret_detected('password = parameters["cred"]')


def test_rejects_bulk_colon_delimited_credentials_in_the_reply() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response="alpha@example.test:FakePass123!\nbeta@example.test:AnotherFakePass456!",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes


def test_does_not_hard_block_on_prior_global_context_secret() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response="I drafted a safe workflow.",
        global_llm_context='{"prior_note": "User previously pasted password: hunter2."}',
    )

    assert verdict.allowed


def test_rejects_raw_secret_in_a_reply_that_also_contains_a_jinja_placeholder() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        user_response="Use {{ parameters.username }} and password: hunter2.",
    )

    assert not verdict.allowed
    assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes


def test_generic_request_policy_clarification_does_not_adjudicate_reply() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(user_response_policy="ask_clarification", allow_update_workflow=False),
        response_type="REPLY",
        user_response="Here is the requested explanation.",
    )

    assert verdict.allowed
    assert verdict.reason_codes == []


def test_output_field_confirmation_is_not_interactively_adjudicated() -> None:
    policy = _policy(
        user_response_policy="proceed",
        completion_contract_status="present",
        completion_criteria=[
            SimpleNamespace(id="record_identity", outcome="The returned record identifies the target record."),
        ],
    )

    verdict = evaluate_output_policy(
        request_policy=policy,
        response_type="ASK_QUESTION",
        user_response="Please confirm the output fields before I build this record status workflow.",
        has_workflow_proposal=False,
        workflow_attempted=False,
    )
    hard = hard_block_output_policy_verdict(verdict)

    assert verdict.allowed
    assert hard.allowed


def test_output_field_confirmation_allowed_when_request_policy_requires_clarification() -> None:
    policy = _policy(
        user_response_policy="ask_clarification",
        clarification_question="Which saved credential should I use?",
        clarification_reason="credential_name_unresolved",
        completion_contract_status="present",
        completion_criteria=[
            SimpleNamespace(id="record_identity", outcome="The returned record identifies the target record."),
        ],
    )

    verdict = evaluate_output_policy(
        request_policy=policy,
        response_type="ASK_QUESTION",
        user_response="Which saved credential should I use?",
        has_workflow_proposal=False,
        workflow_attempted=False,
    )

    assert verdict.allowed is True


def test_url_clarification_does_not_trigger_output_field_confirmation_guard() -> None:
    policy = _policy(user_response_policy="proceed", completion_contract_status="present")

    verdict = evaluate_output_policy(
        request_policy=policy,
        response_type="ASK_QUESTION",
        user_response="Which URL should I use for this workflow?",
        has_workflow_proposal=False,
        workflow_attempted=False,
    )

    assert verdict.allowed


def test_allows_workflow_delivery_language_after_failed_workflow_attempt() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response="I tested the draft, but the test failed: Login rejected.",
        has_workflow_proposal=False,
        workflow_attempted=True,
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
        == CopilotOutputKind.WORKFLOW_RUN_RESULT
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
        reason_codes=[OutputPolicyReason.RAW_SECRET_LEAK],
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
async def test_sdk_input_guardrail_trips_only_on_raw_secret_safety_block() -> None:
    from agents import GuardrailFunctionOutput, InputGuardrail
    from agents.run_context import RunContextWrapper

    input_guardrails = agent_module._build_copilot_input_guardrails(InputGuardrail, GuardrailFunctionOutput)
    result = await input_guardrails[0].run(
        SimpleNamespace(),
        "input",
        RunContextWrapper(
            context=_ctx(
                request_policy=_policy(
                    user_response_policy="ask_clarification",
                    raw_secret_detected=True,
                    raw_secret_handling="block",
                )
            )
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
        "raw_would_have_failed": True,
        "contained_failure": True,
        "final_output_policy_allowed": False,
    }


def test_actuation_adjudicator_symbols_are_deleted() -> None:
    assert not hasattr(output_policy_module, "evaluate_actuation_obligation")
    assert not hasattr(output_policy_module, "request_policy_requires_actuation")
    assert not hasattr(output_policy_module, "actuation_obligation_key")


def test_sdk_output_guardrail_does_not_adjudicate_actuation_claim() -> None:
    ctx = _ctx(
        request_policy=_defer_authoring_policy(),
        tool_activity=[{"tool": "evaluate"}, {"tool": "get_browser_screenshot"}],
    )

    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        ctx,
        {"type": "REPLY", "user_response": "I can fill those fields for you."},
    )

    assert response_type == "REPLY"
    assert verdict.allowed
    assert "actuation_obligation_reason_code" not in diagnostics


@pytest.mark.asyncio
async def test_sdk_output_guardrail_does_not_trip_for_actuation_claim() -> None:
    ctx = _ctx(request_policy=_defer_authoring_policy())
    output_guardrails = agent_module._build_copilot_output_guardrails(OutputGuardrail, GuardrailFunctionOutput)
    result = await output_guardrails[0].run(
        RunContextWrapper(context=ctx),
        SimpleNamespace(),
        {"type": "REPLY", "user_response": "I can fill those fields for you."},
    )

    assert result.output.tripwire_triggered is False
    assert "actuation_obligation_reason_code" not in result.output.output_info


def test_sdk_output_guardrail_does_not_terminalize_historical_actuation_trace() -> None:
    ctx = _ctx(
        request_policy=_defer_authoring_policy(),
        prior_turn_outcome=TurnOutcome(
            response_kind=ResponseKind.CLARIFY,
            reason_code="actuation_obligation_steer",
            actuation_obligation_key="browser_state:build:no_update:no_run",
        ),
    )
    output = {"type": "REPLY", "user_response": "I can fill those fields for you."}

    verdict, _, diagnostics = agent_module._evaluate_copilot_final_output_policy(ctx, output)

    assert verdict.allowed
    assert "actuation_obligation_status" not in diagnostics


def test_sdk_output_guardrail_ignores_model_typed_cannot_act_reason() -> None:
    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        _ctx(request_policy=_defer_authoring_policy()),
        {
            "type": "REPLY",
            "user_response": "I need the field value before I can continue.",
            "cannot_act_reason": "missing_field_value",
        },
    )

    assert response_type == "REPLY"
    assert verdict.allowed
    assert "cannot_act_reason" not in diagnostics


def test_sdk_output_guardrail_allows_generic_report_blocker_reason() -> None:
    ctx = _ctx(request_policy=_defer_authoring_policy())
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text="Report the blocker.",
        user_facing_reason="The page is not ready.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="tool_error_late_block_running",
    )

    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        ctx,
        {"type": "REPLY", "user_response": "The page is not ready."},
    )

    assert response_type == "REPLY"
    assert verdict.allowed
    assert "cannot_act_reason" not in diagnostics


def test_sdk_output_guardrail_allows_successful_mutating_browser_action() -> None:
    ctx = _ctx(request_policy=_defer_authoring_policy())
    ctx.scout_trajectory.append({"tool_name": "click"})

    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        ctx,
        {"type": "REPLY", "user_response": "Clicked the field."},
    )

    assert response_type == "REPLY"
    assert verdict.allowed
    assert "successful_mutating_browser_actions" not in diagnostics


def test_sdk_output_guardrail_does_not_grade_form_fill_click_only_reply() -> None:
    ctx = _ctx(request_policy=_live_fill_policy())
    ctx.scout_trajectory.append({"tool_name": "click"})

    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        ctx,
        {"type": "REPLY", "user_response": "The requested form interaction was completed."},
    )

    assert response_type == "REPLY"
    assert verdict.allowed
    assert "actuation_obligation_reason_code" not in diagnostics


def test_classifier_browser_state_does_not_create_actuation_obligation() -> None:
    ctx = _ctx(
        request_policy=_live_fill_policy(),
    )
    ctx.scout_trajectory.append({"tool_name": "click"})

    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        ctx,
        {"type": "REPLY", "user_response": "The requested form interaction was completed."},
    )

    assert response_type == "REPLY"
    assert verdict.allowed
    assert "durable_fill_required" not in diagnostics


def test_sdk_output_guardrail_allows_non_form_click_reply() -> None:
    ctx = _ctx()
    ctx.scout_trajectory.append({"tool_name": "click"})

    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        ctx,
        {"type": "REPLY", "user_response": "Clicked the requested target."},
    )

    assert response_type == "REPLY"
    assert verdict.allowed
    assert "durable_fill_required" not in diagnostics


def test_sdk_output_guardrail_allows_live_fill_after_durable_fill() -> None:
    ctx = _ctx(request_policy=_live_fill_policy())
    ctx.scout_trajectory.append({"tool_name": "type_text", "typed_length": 3})

    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        ctx,
        {"type": "REPLY", "user_response": "The requested form interaction was completed."},
    )

    assert response_type == "REPLY"
    assert verdict.allowed
    assert "actuation_obligation_status" not in diagnostics


def test_sdk_output_guardrail_does_not_grade_noop_type_text() -> None:
    ctx = _ctx(request_policy=_live_fill_policy())
    ctx.scout_trajectory.append({"tool_name": "type_text", "typed_length": 0, "typed_value": ""})

    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        ctx,
        {"type": "REPLY", "user_response": "The requested form interaction was completed."},
    )

    assert response_type == "REPLY"
    assert verdict.allowed
    assert "actuation_obligation_reason_code" not in diagnostics


def test_sdk_output_guardrail_allows_unknown_click_with_authority_denied_blocker() -> None:
    ctx = _ctx(request_policy=_defer_authoring_policy())
    ctx.scout_trajectory.append({"tool_name": "click"})
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text="Use browser tools.",
        user_facing_reason="I'll respond with the information I already have.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="request_policy_blocks_update_workflow",
        blocked_tool="update_and_run_blocks",
        classifier_mode="unknown",
    )

    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(
        ctx,
        {"type": "REPLY", "user_response": "I'll respond with the information I already have."},
    )

    assert response_type == "REPLY"
    assert verdict.allowed
    assert "actuation_obligation_status" not in diagnostics


def test_output_policy_block_turn_outcome_persists_and_logs_every_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(agent_module, "LOG", log)
    reasons = [
        OutputPolicyReason.PERSISTENCE_STATE_MISMATCH,
        OutputPolicyReason.OUTPUT_POLICY_CONTEXT_MISSING,
    ]

    result = agent_module._build_output_policy_blocked_result(
        _ctx(),
        OutputPolicyVerdict(reason_codes=reasons),
        prior_global_llm_context="{}",
        prior_workflow_yaml="title: Prior",
    )

    assert result.turn_outcome is not None
    assert result.turn_outcome.output_policy_reasons == reasons
    log.warning.assert_any_call(
        "copilot output policy blocked final output",
        log_code="copilot_output_policy_block",
        **{"copilot.output_policy_reasons": [reason.value for reason in reasons]},
    )


def test_unblocked_turn_outcome_has_empty_output_policy_reasons() -> None:
    outcome = TurnOutcome(response_kind=ResponseKind.ANSWER)

    assert outcome.output_policy_reasons == []
    assert outcome.model_dump(mode="json")["output_policy_reasons"] == []


def test_turn_outcome_drops_unknown_historical_output_policy_reasons() -> None:
    outcome = TurnOutcome.model_validate(
        {
            "response_kind": "clarify",
            "reason_code": "output_policy_block",
            "terminal_reason": "output_policy_block",
            "output_policy_reasons": [
                "internal_tool_instruction_leak",
                "credential_scope_broadened",
                OutputPolicyReason.RAW_SECRET_LEAK.value,
            ],
        }
    )

    assert outcome.response_kind is ResponseKind.CLARIFY
    assert outcome.terminal_reason == "output_policy_block"
    assert outcome.output_policy_reasons == [OutputPolicyReason.RAW_SECRET_LEAK]


def test_output_policy_fallback_replacement_keeps_turn_outcome_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    ctx.last_test_anti_bot = "challenge"
    reasons = [OutputPolicyReason.PERSISTENCE_STATE_MISMATCH]
    monkeypatch.setattr(
        agent_module,
        "evaluate_output_policy",
        lambda **_kwargs: OutputPolicyVerdict(reason_codes=[OutputPolicyReason.RAW_SECRET_LEAK]),
    )

    result = agent_module._build_output_policy_blocked_result(
        ctx,
        OutputPolicyVerdict(reason_codes=reasons),
        prior_global_llm_context="{}",
        prior_workflow_yaml="title: Prior",
    )

    assert result.turn_outcome is not None
    assert result.turn_outcome.output_policy_reasons == reasons
    assert result.turn_outcome.model_dump(mode="json")["output_policy_reasons"] == [reason.value for reason in reasons]


def test_output_policy_block_preserves_already_gated_workflow_proposal() -> None:
    ctx = _ctx()
    ctx.last_workflow = SimpleNamespace(name="draft")
    ctx.last_workflow_yaml = _COVERED_DRAFT_YAML
    ctx.executed_block_fingerprints = {
        label: set(values) for label, values in workflow_block_fingerprints(_COVERED_DRAFT_YAML).items()
    }
    ctx.workflow_persisted = True
    ctx.last_test_ok = True

    result = agent_module._build_output_policy_blocked_result(
        ctx,
        OutputPolicyVerdict(
            reason_codes=[OutputPolicyReason.PERSISTENCE_STATE_MISMATCH],
        ),
        prior_global_llm_context="{}",
        prior_workflow_yaml="title: Prior",
    )

    assert result.response_type == "ASK_QUESTION"
    assert result.updated_workflow is ctx.last_workflow
    assert result.workflow_yaml == _COVERED_DRAFT_YAML
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


def test_sdk_output_guardrail_ignores_internal_context_credential_ids() -> None:
    ctx = _ctx(
        request_policy=_policy(
            credential_input_kind="credential_name",
            credential_refs=["azure_credentials"],
            allow_run_blocks=False,
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
    assert diagnostics["raw_would_have_failed"] is False
    assert diagnostics["contained_failure"] is False


def test_inline_replace_output_policy_turn_outcome_keeps_raw_reasons_before_processing(monkeypatch) -> None:
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
    assert agent_result.turn_outcome is not None
    assert agent_result.turn_outcome.output_policy_reasons == [OutputPolicyReason.RAW_SECRET_LEAK]
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


def test_translate_preserves_draft_on_late_block_running_blocker() -> None:
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


def test_output_policy_allows_informational_prose_about_a_completed_workflow() -> None:
    verdict = evaluate_output_policy(
        request_policy=_policy(),
        response_type="REPLY",
        user_response=(
            "The workflow runs as generated code. This explanation does not mean the workflow is complete. "
            "Earlier I said the workflow is ready; that was before the test failed."
        ),
        has_workflow_proposal=False,
        workflow_attempted=False,
    )

    assert verdict.allowed
    assert verdict.reason_codes == []
    assert verdict.output_kind == CopilotOutputKind.INFORMATIONAL_ANSWER


def test_output_policy_guardrail_turn_outcome_recovers_every_raw_reason() -> None:
    output_info = {
        "allowed": False,
        "reason_codes": [OutputPolicyReason.RAW_SECRET_LEAK.value],
        "raw_reason_codes": [
            OutputPolicyReason.RAW_SECRET_LEAK.value,
            OutputPolicyReason.PERSISTENCE_STATE_MISMATCH.value,
        ],
    }
    exc = SimpleNamespace(guardrail_result=SimpleNamespace(output=SimpleNamespace(output_info=output_info)))

    reasons = agent_module._output_policy_reason_codes_from_guardrail_exception(exc)

    assert reasons == [
        OutputPolicyReason.RAW_SECRET_LEAK,
        OutputPolicyReason.PERSISTENCE_STATE_MISMATCH,
    ]


@pytest.mark.parametrize(
    "reply",
    [
        "Don't use `task_v2` in a new workflow. It is a legacy block; use `navigation` or `code` blocks instead.",
        'Type "https://example.com" in the browser address field, then press Enter.',
        (
            "A loop block runs the blocks inside it once for each item in a list:\n\n"
            "```yaml\n"
            "- block_type: for_loop\n"
            "  label: visit_each_url\n"
            "  loop_over_parameter_key: urls\n"
            "```\n"
        ),
    ],
)
@pytest.mark.parametrize("has_proposal", [False, True])
def test_translate_to_agent_result_returns_the_reply_the_model_wrote(reply: str, has_proposal: bool) -> None:
    ctx = _ctx()
    workflow = SimpleNamespace(name="draft") if has_proposal else None
    if has_proposal:
        ctx.last_workflow = workflow
        ctx.last_workflow_yaml = _workflow_yaml()
        ctx.last_test_ok = True

    agent_result = asyncio.run(
        agent_module._translate_to_agent_result(
            _fake_run_result({"type": "REPLY", "user_response": reply}),
            ctx,
            global_llm_context=None,
            chat_request=_chat_request(),
            organization_id="org-1",
        )
    )

    assert agent_result.user_response == reply
    assert agent_result.updated_workflow is workflow


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


def _present_contract_confirmation_policy() -> RequestPolicy:
    return _policy(
        user_response_policy="proceed",
        completion_contract_status="present",
        completion_criteria=[
            SimpleNamespace(id="record_identity", outcome="The returned record identifies the target record."),
        ],
    )


def test_genuine_attempt_output_schema_ask_is_allowed() -> None:
    verdict = evaluate_output_policy(
        request_policy=_present_contract_confirmation_policy(),
        response_type="ASK_QUESTION",
        user_response="Please confirm the output fields before I build this record status workflow.",
        has_workflow_proposal=False,
        workflow_attempted=True,
    )

    assert verdict.allowed


class _CaptureSentinel(Exception):
    pass


def _run_id_only_ctx() -> CopilotContext:
    ctx = _ctx()
    ctx.last_run_blocks_workflow_run_id = "wr_discriminating"
    ctx.dispatched_run_ids_this_turn.add("wr_discriminating")
    ctx.last_update_block_count = None
    ctx.last_test_ok = None
    ctx.last_workflow = None
    return ctx


def test_an_inherited_run_id_is_not_a_genuine_workflow_attempt() -> None:
    ctx = _run_id_only_ctx()
    ctx.dispatched_run_ids_this_turn.clear()

    assert ctx.has_genuine_workflow_attempt() is False


def test_build_exit_result_passes_genuine_predicate_as_workflow_attempted(monkeypatch) -> None:
    ctx = _run_id_only_ctx()
    assert ctx.has_genuine_workflow_attempt() is True

    captured: dict[str, Any] = {}

    def _capture_eval(**kwargs: Any) -> None:
        captured["evaluate"] = kwargs["workflow_attempted"]
        raise _CaptureSentinel

    def _capture_kind(**kwargs: Any) -> None:
        captured["derive"] = kwargs["workflow_attempted"]

    monkeypatch.setattr(agent_module, "derive_output_kind", _capture_kind)
    monkeypatch.setattr(agent_module, "evaluate_output_policy", _capture_eval)

    with pytest.raises(_CaptureSentinel):
        agent_module._build_exit_result(ctx, user_response="Done.", global_llm_context=None)

    assert captured["derive"] is True
    assert captured["evaluate"] is True
    assert captured["evaluate"] == ctx.has_genuine_workflow_attempt()


_AVOIDABLE_ASK = {
    "type": "ASK_QUESTION",
    "user_response": "Please confirm the output fields before I build this workflow.",
}


def _present_contract_ask_policy(**overrides: object) -> RequestPolicy:
    defaults: dict[str, object] = dict(
        user_response_policy="proceed",
        clarification_reason="none",
        completion_contract_status="present",
        completion_criteria=[
            CompletionCriterion(id="record_id", outcome="The returned record includes the requested id."),
        ],
    )
    defaults.update(overrides)
    return _policy(**defaults)


def _recoverable_ask_ctx(
    *,
    policy: RequestPolicy | None = None,
    **marker: object,
) -> CopilotContext:
    ctx = _ctx(request_policy=policy or _present_contract_ask_policy())
    for name, value in marker.items():
        setattr(ctx, name, value)
    return ctx


def test_ask_passes_without_output_policy_deferral_or_recycle() -> None:
    ctx = _recoverable_ask_ctx()

    verdict, response_type, diagnostics = agent_module._evaluate_copilot_final_output_policy(ctx, _AVOIDABLE_ASK)

    assert response_type == "ASK_QUESTION"
    assert verdict.allowed is True
    assert diagnostics.get("deferred_to_recycle", False) is False
    assert diagnostics["final_output_policy_allowed"] is True


@pytest.mark.asyncio
async def test_sdk_output_guardrail_allows_output_schema_ask_without_tripwire() -> None:
    output_guardrails = agent_module._build_copilot_output_guardrails(OutputGuardrail, GuardrailFunctionOutput)
    result = await output_guardrails[0].run(
        RunContextWrapper(context=_recoverable_ask_ctx()),
        SimpleNamespace(),
        _AVOIDABLE_ASK,
    )

    assert result.output.tripwire_triggered is False
    assert result.output.output_info.get("deferred_to_recycle", False) is False


@pytest.mark.parametrize(
    ("policy_overrides", "marker", "policy_may_author", "_expected_admit"),
    [
        pytest.param({}, {}, True, True, id="recoverable_admits"),
        pytest.param({}, {"test_after_update_done": True}, True, True, id="scout_only_admits"),
        pytest.param({}, {"last_test_ok": True}, True, False, id="genuine_test"),
        pytest.param({}, {"last_run_blocks_workflow_run_id": "wr_1"}, True, False, id="genuine_run"),
        pytest.param({"user_response_policy": "ask_clarification"}, {}, True, False, id="ask_clarification"),
        pytest.param(
            {"clarification_reason": "credential_name_unresolved"}, {}, True, False, id="clarification_reason"
        ),
        pytest.param({}, {}, False, False, id="non_authoring"),
    ],
)
def test_output_schema_ask_never_enters_deleted_recycle_plane(
    policy_overrides: dict[str, object],
    marker: dict[str, object],
    policy_may_author: bool,
    _expected_admit: bool,
) -> None:
    policy = _present_contract_ask_policy(**policy_overrides)
    policy.allow_update_workflow = policy_may_author
    policy.allow_run_blocks = policy_may_author
    ctx = _recoverable_ask_ctx(
        policy=policy,
        **marker,
    )

    _, _, diagnostics = agent_module._evaluate_copilot_final_output_policy(ctx, _AVOIDABLE_ASK)
    assert diagnostics.get("deferred_to_recycle", False) is False


@pytest.mark.parametrize(
    ("policy_overrides", "policy_may_author"),
    [
        pytest.param({"clarification_reason": "credential_name_unresolved"}, True, id="clarification_reason"),
        pytest.param({}, False, id="non_authoring"),
    ],
)
def test_deleted_backstop_does_not_block_output_schema_ask(
    policy_overrides: dict[str, object],
    policy_may_author: bool,
) -> None:
    policy = _present_contract_ask_policy(**policy_overrides)
    policy.allow_update_workflow = policy_may_author
    policy.allow_run_blocks = policy_may_author
    ctx = _recoverable_ask_ctx(
        policy=policy,
    )

    verdict, _, diagnostics = agent_module._evaluate_copilot_final_output_policy(ctx, _AVOIDABLE_ASK)

    assert verdict.allowed is True
    assert diagnostics.get("deferred_to_recycle", False) is False


def test_avoidable_ask_recycle_helper_is_deleted() -> None:
    assert not hasattr(agent_module, "_defer_avoidable_ask_to_recycle")


def test_output_schema_ask_with_secret_leak_keeps_only_the_safety_block() -> None:
    ctx = _recoverable_ask_ctx()
    ask = {
        "type": "ASK_QUESTION",
        "user_response": (
            "Please confirm the output fields before I build this workflow. I used password: hunter2 to test the login."
        ),
    }

    verdict, _, diagnostics = agent_module._evaluate_copilot_final_output_policy(ctx, ask)

    assert verdict.allowed is False
    assert OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes
    assert diagnostics.get("deferred_to_recycle", False) is False


# Reads as "does not refuse", not "reaches data['findings']": only schema_incompatibility,
# code_artifact_metadata_incomplete and synthesized_code_block_not_imposed populate that bucket.
_FINDING = "finding"

# Enumerated by hand on purpose: deriving it from the enum would auto-classify a new refusal
# source and defeat the ratchet. See architecture/output-policy-disposition.md.
_AUTHORING_SEAM_REFUSAL_SOURCES: dict[str, str] = {
    "code_safety": CODE_SAFETY_BLOCK_ID,
    "credential_scout": CREDENTIAL_SCOUT_BLOCK_ID,
    "banned_blocks": BANNED_BLOCKS_BLOCK_ID,
    "raw_secret_leak": _FINDING,
    "unapproved_credential_reference": CREDENTIAL_SCOUT_BLOCK_ID,
    "persistence_state_mismatch": _FINDING,
    "output_policy_context_missing": _FINDING,
}


def test_every_authoring_seam_refusal_source_is_classified() -> None:
    """An unclassified refusal source fails here rather than becoming a wall in production."""
    sources = {reason.value for reason in OutputPolicyReason} | set(AUTHOR_TIME_HARD_BLOCKS)

    assert sources == set(_AUTHORING_SEAM_REFUSAL_SOURCES)
    for source, disposition in _AUTHORING_SEAM_REFUSAL_SOURCES.items():
        assert disposition in AUTHOR_TIME_HARD_BLOCKS or disposition == _FINDING, source


@pytest.mark.parametrize("source", sorted(_AUTHORING_SEAM_REFUSAL_SOURCES))
def test_only_a_hard_block_identity_can_refuse(source: str) -> None:
    disposition = _AUTHORING_SEAM_REFUSAL_SOURCES[source]
    if disposition == _FINDING:
        with pytest.raises(ValueError):
            AuthorTimeBlock(block_id=source, error="steer")
        return
    assert AuthorTimeBlock(block_id=disposition, error="refuse").block_id in AUTHOR_TIME_HARD_BLOCKS


@pytest.mark.parametrize(
    ("reply", "label"),
    [
        pytest.param("Use password: hunter2 next time", "password", id="keyword"),
        pytest.param("Use API key: sk_live_value here", "API key", id="spaced_keyword"),
        pytest.param("See eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.cccc_password=hunter2 done", "password", id="jwt_straddle"),
        pytest.param("Log in with Summer2024_password: x", "password", id="prefixed_keyword"),
        pytest.param("Set db_api_key=abc123def", "api_key", id="prefixed_underscored_keyword"),
        pytest.param("Your otp code is 482913", "otp code", id="otp_phrase"),
        pytest.param("Use 2FA 48291307 now", "2FA", id="otp_bare"),
        pytest.param("Your otp: 482913", "otp", id="otp_label"),
        pytest.param(f"Session {_FAKE_JWT} is live", "", id="jwt"),
        pytest.param("Saved rows: alice@example.com:Pa55word-xyz then more", "", id="email_pair"),
        pytest.param("Use password: {{ credentials.password }} at login", None, id="placeholder_only"),
    ],
)
def test_raw_secret_label_is_the_detector_label_and_never_the_value(reply: str, label: str | None) -> None:
    assert raw_secret_label(reply) == label

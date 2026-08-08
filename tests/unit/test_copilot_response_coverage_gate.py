"""Tests for response-aware enforcement decisions.

The response peek still rejects delivery claims without a workflow and
progress narration, but demonstrated outcomes are terminal regardless of
how many user actions one executable block covers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.enforcement import (
    MAX_DISCOVERY_ENTRYPOINT_URL_QUESTION_NUDGES,
    MAX_FORMAT_NUDGES,
    MAX_NO_WORKFLOW_NUDGES,
    _is_progress_narration,
    _response_output_nudge,
    enforcement_decision,
    verified_goal_satisfied_context,
)


class _Ctx:
    """Minimal stand-in for CopilotContext used in enforcement checks."""

    def __init__(self) -> None:
        self.navigate_called = False
        self.observation_after_navigate = False
        self.navigate_enforcement_done = False
        self.update_workflow_called = False
        self.test_after_update_done = False
        self.post_update_nudge_count = 0
        self.format_nudge_count = 0
        self.no_workflow_nudge_count = 0
        self.discovery_entrypoint_url_question_nudge_count = 0
        self.user_message = ""
        self.request_policy = None
        self.completion_criteria_turn_state = None
        self.completion_verification_result = None
        self.resolved_discovery_entrypoint_url = None
        self.resolved_discovery_failure_reason = None
        self.resolved_discovery_entrypoint_inspection_baseline = 0
        self.page_inspection_calls_this_turn = 0
        self.composition_page_evidence = None
        self.last_update_block_count = None
        self.last_test_ok = None
        self.latest_diagnosis_repair_contract = None
        self.last_full_workflow_test_ok = False
        self.last_test_failure_reason = None
        self.last_test_suspicious_success = False
        self.last_test_anti_bot = None
        self.failed_test_nudge_count = 0
        self.explore_without_workflow_nudge_count = 0
        self.repeated_failure_streak_count = 0
        self.repeated_failure_nudge_emitted_at_streak = 0
        self.last_artifact_health_blocker_reason = None
        self.completion_verification_result = None
        self.copilot_total_timeout_exceeded = False


@dataclass
class _FakeRunResult:
    """Stand-in for RunResultStreaming — exposes only what extract_final_text uses."""

    final_output: Any = None
    new_items: list[Any] = field(default_factory=list)


def _reply_result(user_response: str) -> _FakeRunResult:
    return _FakeRunResult(
        final_output=json.dumps({"type": "REPLY", "user_response": user_response}),
    )


def _ask_question_result(question: str) -> _FakeRunResult:
    return _FakeRunResult(
        final_output=json.dumps({"type": "ASK_QUESTION", "user_response": question}),
    )


def _post_success_ctx(user_message: str, block_count: int = 1) -> _Ctx:
    """Build a ctx in the 'workflow test passed' state that would previously
    have triggered the intermediate-success nudge."""
    ctx = _Ctx()
    ctx.user_message = user_message
    ctx.update_workflow_called = True
    ctx.test_after_update_done = True
    ctx.last_test_ok = True
    ctx.last_update_block_count = block_count
    return ctx


# ---------------------------------------------------------------------------
# _response_output_nudge — direct unit tests
# ---------------------------------------------------------------------------


def test_reply_after_success_with_request_policy_completion_contract_passes_through() -> None:
    ctx = _post_success_ctx("Go to https://example.com/contact. Fill out the contact form and submit it.")
    ctx.request_policy = SimpleNamespace(completion_contract="confirmation banner appears")
    parsed = {"type": "REPLY", "user_response": "I created and tested the workflow."}

    assert _response_output_nudge(ctx, parsed) is None


def test_reply_after_success_with_unknown_fallback_contract_passes_through() -> None:
    ctx = _post_success_ctx("Go to https://example.com/contact. Fill out the contact form and submit it.")
    ctx.request_policy = SimpleNamespace(completion_contract_status="unknown")
    parsed = {"type": "REPLY", "user_response": "I created and tested the workflow."}

    assert _response_output_nudge(ctx, parsed) is None


def test_ask_question_passes_through_after_success() -> None:
    ctx = _post_success_ctx("go to site and download file")
    parsed = {"type": "ASK_QUESTION", "user_response": "Which file do you mean?"}
    assert _response_output_nudge(ctx, parsed) is None


def test_ask_question_before_acting_on_discovery_candidate_fires_nudge() -> None:
    ctx = _Ctx()
    ctx.resolved_discovery_entrypoint_url = "https://example.com/"
    parsed = {
        "type": "ASK_QUESTION",
        "user_response": "Which file should I download?",
    }

    nudge = _response_output_nudge(ctx, parsed)

    assert nudge is not None
    assert nudge.rule == "post_discovery_entrypoint_url_question"
    # nosemgrep false positive: asserts the advisory interpolates the resolved entrypoint.
    assert (
        nudge.message.rpartition("Resolved candidate_url: ")[2] == ctx.resolved_discovery_entrypoint_url
    )  # nosemgrep: incomplete-url-substring-sanitization
    assert ctx.discovery_entrypoint_url_question_nudge_count == 1


def test_ask_question_after_discovery_failure_still_passes_through() -> None:
    ctx = _Ctx()
    ctx.resolved_discovery_entrypoint_url = "https://example.com/"
    ctx.resolved_discovery_failure_reason = "could_not_resolve_site_name"
    parsed = {"type": "ASK_QUESTION", "user_response": "Which URL should I use?"}

    assert _response_output_nudge(ctx, parsed) is None


def test_ask_question_before_acting_on_discovery_candidate_caps_nudges() -> None:
    ctx = _Ctx()
    ctx.resolved_discovery_entrypoint_url = "https://example.com/"
    parsed = {"type": "ASK_QUESTION", "user_response": "Which file should I download?"}

    for expected_count in range(1, MAX_DISCOVERY_ENTRYPOINT_URL_QUESTION_NUDGES + 1):
        assert _response_output_nudge(ctx, parsed) is not None
        assert ctx.discovery_entrypoint_url_question_nudge_count == expected_count
    assert _response_output_nudge(ctx, parsed) is None
    assert ctx.discovery_entrypoint_url_question_nudge_count == MAX_DISCOVERY_ENTRYPOINT_URL_QUESTION_NUDGES


def test_ask_question_after_page_inspection_of_discovery_candidate_passes_through() -> None:
    ctx = _Ctx()
    ctx.resolved_discovery_entrypoint_url = "https://example.com/"
    ctx.page_inspection_calls_this_turn = 1
    ctx.composition_page_evidence = {
        "source_tool": "inspect_page_for_composition",
        "inspected_url": "https://example.com/",
        "current_url": "https://example.com/",
    }
    parsed = {"type": "ASK_QUESTION", "user_response": "Which account should I use?"}

    assert _response_output_nudge(ctx, parsed) is None


def test_ask_question_after_unrelated_page_inspection_of_discovery_candidate_still_fires_nudge() -> None:
    ctx = _Ctx()
    ctx.resolved_discovery_entrypoint_url = "https://example.com/"
    ctx.page_inspection_calls_this_turn = 1
    ctx.composition_page_evidence = {
        "source_tool": "inspect_page_for_composition",
        "inspected_url": "https://other.example/",
        "current_url": "https://other.example/",
    }
    parsed = {"type": "ASK_QUESTION", "user_response": "Which account should I use?"}

    assert _response_output_nudge(ctx, parsed) is not None


def test_ask_question_after_stale_candidate_page_inspection_still_fires_nudge() -> None:
    ctx = _Ctx()
    ctx.resolved_discovery_entrypoint_url = "https://example.com/"
    ctx.resolved_discovery_entrypoint_inspection_baseline = 1
    ctx.page_inspection_calls_this_turn = 1
    ctx.composition_page_evidence = {
        "source_tool": "inspect_page_for_composition",
        "inspected_url": "https://example.com/",
        "current_url": "https://example.com/",
    }
    parsed = {"type": "ASK_QUESTION", "user_response": "Which account should I use?"}

    assert _response_output_nudge(ctx, parsed) is not None


def test_ask_question_after_mutating_from_discovery_candidate_passes_through() -> None:
    ctx = _Ctx()
    ctx.resolved_discovery_entrypoint_url = "https://example.com/"
    ctx.update_workflow_called = True
    parsed = {"type": "ASK_QUESTION", "user_response": "Which account should I use?"}

    assert _response_output_nudge(ctx, parsed) is None


def test_clean_reply_after_success_passes_through() -> None:
    ctx = _post_success_ctx("go to X and download Y", block_count=2)
    parsed = {"type": "REPLY", "user_response": "Done. I created a 2-block workflow."}
    assert _response_output_nudge(ctx, parsed) is None


def test_reply_before_any_successful_test_passes_through() -> None:
    ctx = _Ctx()
    ctx.user_message = "go to X and download Y"
    # last_test_ok is None — no successful test yet.
    parsed = {"type": "REPLY", "user_response": "Working on it."}
    assert _response_output_nudge(ctx, parsed) is None


def test_reply_claiming_workflow_without_update_fires_nudge() -> None:
    ctx = _Ctx()
    parsed = {"type": "REPLY", "user_response": "Here's the workflow."}

    assert _response_output_nudge(ctx, parsed).rule == "post_no_workflow_delivery"
    assert ctx.no_workflow_nudge_count == 1


def test_initial_part_workflow_claim_without_update_fires_nudge() -> None:
    ctx = _Ctx()
    parsed = {
        "type": "REPLY",
        "user_response": "In the meantime, I've drafted the initial part of your workflow with placeholders.",
    }

    assert _response_output_nudge(ctx, parsed).rule == "post_no_workflow_delivery"
    assert ctx.no_workflow_nudge_count == 1


def test_no_workflow_delivery_nudge_respects_counter_cap() -> None:
    ctx = _Ctx()
    parsed = {"type": "REPLY", "user_response": "I created a workflow for this."}

    for _ in range(MAX_NO_WORKFLOW_NUDGES):
        assert _response_output_nudge(ctx, parsed).rule == "post_no_workflow_delivery"
    assert _response_output_nudge(ctx, parsed) is None


def test_no_workflow_delivery_nudge_ignores_existing_update_path() -> None:
    ctx = _Ctx()
    ctx.update_workflow_called = True
    parsed = {"type": "REPLY", "user_response": "Here's the workflow."}

    assert _response_output_nudge(ctx, parsed) is None


def test_reply_after_failed_test_passes_through() -> None:
    ctx = _post_success_ctx("go to X and download Y")
    ctx.last_test_ok = False  # test failed
    parsed = {"type": "REPLY", "user_response": "The test failed."}
    assert _response_output_nudge(ctx, parsed) is None


# ---------------------------------------------------------------------------
# Progress-narration heuristic
# ---------------------------------------------------------------------------


def test_is_progress_narration_detects_future_tense() -> None:
    # Exact phrasing from the regression trace that escaped enforcement.
    text = (
        "I ran the first block (open_home). The navigation block completed. "
        "I did not attempt further blocks yet. Next I will proceed to run the "
        "remaining blocks to locate and download the regulations unless "
        "you want a change."
    )
    assert _is_progress_narration(text)


def test_is_progress_narration_ignores_clean_reply() -> None:
    assert not _is_progress_narration("I created a 2-block workflow that extracts the top posts.")
    assert not _is_progress_narration("The workflow is ready. 3 blocks: nav, extract, summarize.")


def test_is_progress_narration_empty_inputs() -> None:
    assert not _is_progress_narration("")
    assert not _is_progress_narration(None)  # type: ignore[arg-type]


def test_format_nudge_fires_for_progress_narration() -> None:
    ctx = _post_success_ctx("go to X and download Y", block_count=2)
    parsed = {
        "type": "REPLY",
        "user_response": "I ran the first block. Next I will proceed to add the rest.",
    }
    nudge = _response_output_nudge(ctx, parsed)
    assert nudge.rule == "post_format"
    assert ctx.format_nudge_count == 1


def test_format_nudge_respects_counter_cap() -> None:
    ctx = _post_success_ctx("go to X and download Y", block_count=2)
    parsed = {"type": "REPLY", "user_response": "Next I will proceed."}
    for _ in range(MAX_FORMAT_NUDGES):
        assert _response_output_nudge(ctx, parsed).rule == "post_format"
    assert _response_output_nudge(ctx, parsed) is None


def test_progress_narration_nudge_is_independent_of_workflow_block_count() -> None:
    ctx = _post_success_ctx("go to X and download Y", block_count=1)
    parsed = {"type": "REPLY", "user_response": "Next I will proceed with more blocks."}
    assert _response_output_nudge(ctx, parsed).rule == "post_format"
    assert ctx.format_nudge_count == 1


# ---------------------------------------------------------------------------
# Integrated enforcement_decision — no-op-turn bypass closed (main regression)
# ---------------------------------------------------------------------------


def test_ph1_one_block_login_and_extraction_with_requested_output_terminates() -> None:
    """A demonstrated requested output is terminal even when one code block
    covers multiple actions from the user's prompt."""
    observed_azure_errors = 27
    ctx = _post_success_ctx(
        "Log in to Datadog with the saved credential and then extract the number of Azure errors.",
        block_count=1,
    )
    ctx.request_policy = SimpleNamespace(completion_contract="Return the number of Azure errors")
    ctx.last_full_workflow_test_ok = True
    ctx.verified_block_outputs = {
        "login_and_extract": {"azure_error_count": observed_azure_errors},
    }
    ctx.completion_verification_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["azure_error_count"],
        verdicts=[
            CriterionVerdict(
                criterion_id="azure_error_count",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="block_outputs:login_and_extract.azure_error_count",
                output_path="output.azure_error_count",
            )
        ],
    )
    result = _reply_result(f"The workflow ran successfully. Azure errors: {observed_azure_errors}.")

    assert ctx.verified_block_outputs["login_and_extract"]["azure_error_count"] == observed_azure_errors
    assert verified_goal_satisfied_context(ctx) is True
    assert enforcement_decision(ctx, result) is None
    assert ctx.copilot_total_timeout_exceeded is False


def test_no_op_turn_bypass_closed_goes_to_phrasing() -> None:
    """Progress narration remains non-terminal after the block-count veto is deleted."""
    ctx = _post_success_ctx("make a workflow that goes to example.com and downloads the latest regulations")
    result = _reply_result(
        "I ran the first block (open_home). The navigation block completed. "
        "I did not attempt further blocks yet. Next I will proceed."
    )
    nudge = enforcement_decision(ctx, result)
    assert nudge.rule == "post_format"


def test_ask_question_reaches_user_after_any_state() -> None:
    """ASK_QUESTION still reaches the user for credentials or disambiguation."""
    ctx = _post_success_ctx("login and download my records")
    result = _ask_question_result("Which credential should I use for this login?")
    assert enforcement_decision(ctx, result) is None


def test_enforcement_decision_without_result_skips_response_peek() -> None:
    """Pre-screenshot-handoff path passes result=None. State-based branches
    still fire; response peek is skipped."""
    ctx = _Ctx()
    ctx.navigate_called = True  # but no observation_after_navigate
    # navigate_enforcement_done is still False
    nudge = enforcement_decision(ctx, None)
    assert nudge is not None  # navigate nudge fires


def test_enforcement_decision_clean_reply_passes_through() -> None:
    ctx = _post_success_ctx("go to example.com and extract the top 3 stories", block_count=2)
    result = _reply_result("I created a 2-block workflow that extracts the top 3 stories.")
    assert enforcement_decision(ctx, result) is None

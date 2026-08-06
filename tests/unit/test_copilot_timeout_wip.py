"""Tests for the capacity-exhausted WIP carve-outs (timeout, max-turns)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.agent import (
    _CANCEL_REPLY_DEFAULT,
    _CANCEL_REPLY_TESTED,
    _CANCEL_REPLY_UNVALIDATED,
    _MAX_TURNS_REPLY_DEFAULT,
    _MAX_TURNS_REPLY_TESTED,
    _MAX_TURNS_REPLY_UNVALIDATED,
    _TIMEOUT_REPLY_DEFAULT,
    _TIMEOUT_REPLY_TESTED,
    _TIMEOUT_REPLY_UNVALIDATED,
    _UNEXPECTED_ERROR_REPLY_TESTED,
    _UNEXPECTED_ERROR_REPLY_UNVALIDATED,
    _build_cancel_exit_result,
    _build_cancelled_exit_result,
    _build_max_turns_exit_result,
    _build_timeout_exit_result,
    _build_unexpected_error_exit_result,
)
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisInput,
    DiagnosisRepairContract,
    DiagnosisResult,
    RepairDecision,
    VerificationResult,
)


def _ctx(
    *,
    last_workflow: object | None,
    last_workflow_yaml: str | None,
    last_test_ok: bool | None,
    last_test_suspicious_success: bool = False,
    last_good_workflow: object | None = None,
    last_good_workflow_yaml: str | None = None,
) -> MagicMock:
    ctx = MagicMock()
    ctx.last_workflow = last_workflow
    ctx.last_workflow_yaml = last_workflow_yaml
    ctx.last_test_ok = last_test_ok
    ctx.last_full_workflow_test_ok = last_test_ok is True
    ctx.last_test_suspicious_success = last_test_suspicious_success
    ctx.copilot_total_timeout_exceeded = False
    ctx.workflow_persisted = last_workflow is not None
    ctx.total_tokens_used = None
    ctx.last_good_workflow = last_good_workflow
    ctx.last_good_workflow_yaml = last_good_workflow_yaml
    ctx.tool_activity = []
    ctx.latest_diagnosis_repair_contract = None
    ctx.test_after_update_done = last_test_ok is not None
    ctx.last_update_block_count = None
    return ctx


def _blocker_contract(
    reason: str,
    *,
    run_status: str | None = "running",
    workflow_run_id: str | None = None,
) -> DiagnosisRepairContract:
    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(
            source_tool="get_browser_screenshot",
            workflow_run_id=workflow_run_id,
            run_status=run_status,
        ),
        diagnosis_result=DiagnosisResult(root_cause_summary=reason, confidence=0.9),
        repair_decision=RepairDecision(next_action="stop"),
        verification_result=VerificationResult(
            run_status=run_status,
            remaining_blocker=reason,
        ),
    )


def _overwrite_ctx(*, last_test_ok: bool | None) -> MagicMock:
    good = MagicMock(name="wf-good")
    in_flight = MagicMock(name="wf-in-flight")
    return _ctx(
        last_workflow=in_flight,
        last_workflow_yaml="version: in-flight",
        last_test_ok=last_test_ok,
        last_good_workflow=good,
        last_good_workflow_yaml="version: good",
    )


_STATE_EXPECTATIONS = {
    "no_workflow": ("auto_applicable", "default", False),
    "untested": ("review_untested", "unvalidated", True),
    "failed_test": ("auto_applicable", "default", False),
    "passing_test": ("review_tested", "tested", True),
    "suspicious_success": ("auto_applicable", "default", False),
}


def _state_ctx(state_kind: str) -> tuple[MagicMock, object | None]:
    if state_kind == "no_workflow":
        return _ctx(last_workflow=None, last_workflow_yaml=None, last_test_ok=None), None
    wf = MagicMock(name="wf")
    if state_kind == "untested":
        return _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None), wf
    if state_kind == "failed_test":
        return _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=False), wf
    if state_kind == "passing_test":
        return _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=True), wf
    if state_kind == "suspicious_success":
        return (
            _ctx(
                last_workflow=wf,
                last_workflow_yaml="version: '1.0'",
                last_test_ok=None,
                last_test_suspicious_success=True,
            ),
            wf,
        )
    raise ValueError(state_kind)


@pytest.mark.parametrize(
    ("builder", "default_reply", "unvalidated_reply", "tested_reply", "expected_cancelled", "state_kind"),
    [
        pytest.param(
            _build_timeout_exit_result,
            _TIMEOUT_REPLY_DEFAULT,
            _TIMEOUT_REPLY_UNVALIDATED,
            _TIMEOUT_REPLY_TESTED,
            False,
            "no_workflow",
            id="timeout-no_workflow",
        ),
        pytest.param(
            _build_timeout_exit_result,
            _TIMEOUT_REPLY_DEFAULT,
            _TIMEOUT_REPLY_UNVALIDATED,
            _TIMEOUT_REPLY_TESTED,
            False,
            "untested",
            id="timeout-untested",
        ),
        pytest.param(
            _build_timeout_exit_result,
            _TIMEOUT_REPLY_DEFAULT,
            _TIMEOUT_REPLY_UNVALIDATED,
            _TIMEOUT_REPLY_TESTED,
            False,
            "failed_test",
            id="timeout-failed_test",
        ),
        pytest.param(
            _build_timeout_exit_result,
            _TIMEOUT_REPLY_DEFAULT,
            _TIMEOUT_REPLY_UNVALIDATED,
            _TIMEOUT_REPLY_TESTED,
            False,
            "passing_test",
            id="timeout-passing_test",
        ),
        pytest.param(
            _build_timeout_exit_result,
            _TIMEOUT_REPLY_DEFAULT,
            _TIMEOUT_REPLY_UNVALIDATED,
            _TIMEOUT_REPLY_TESTED,
            False,
            "suspicious_success",
            id="timeout-suspicious_success",
        ),
        pytest.param(
            _build_max_turns_exit_result,
            _MAX_TURNS_REPLY_DEFAULT,
            _MAX_TURNS_REPLY_UNVALIDATED,
            _MAX_TURNS_REPLY_TESTED,
            False,
            "no_workflow",
            id="max_turns-no_workflow",
        ),
        pytest.param(
            _build_max_turns_exit_result,
            _MAX_TURNS_REPLY_DEFAULT,
            _MAX_TURNS_REPLY_UNVALIDATED,
            _MAX_TURNS_REPLY_TESTED,
            False,
            "untested",
            id="max_turns-untested",
        ),
        pytest.param(
            _build_max_turns_exit_result,
            _MAX_TURNS_REPLY_DEFAULT,
            _MAX_TURNS_REPLY_UNVALIDATED,
            _MAX_TURNS_REPLY_TESTED,
            False,
            "failed_test",
            id="max_turns-failed_test",
        ),
        pytest.param(
            _build_max_turns_exit_result,
            _MAX_TURNS_REPLY_DEFAULT,
            _MAX_TURNS_REPLY_UNVALIDATED,
            _MAX_TURNS_REPLY_TESTED,
            False,
            "passing_test",
            id="max_turns-passing_test",
        ),
        pytest.param(
            _build_max_turns_exit_result,
            _MAX_TURNS_REPLY_DEFAULT,
            _MAX_TURNS_REPLY_UNVALIDATED,
            _MAX_TURNS_REPLY_TESTED,
            False,
            "suspicious_success",
            id="max_turns-suspicious_success",
        ),
        pytest.param(
            _build_cancel_exit_result,
            _CANCEL_REPLY_DEFAULT,
            _CANCEL_REPLY_UNVALIDATED,
            _CANCEL_REPLY_TESTED,
            True,
            "no_workflow",
            id="cancel-no_workflow",
        ),
        pytest.param(
            _build_cancel_exit_result,
            _CANCEL_REPLY_DEFAULT,
            _CANCEL_REPLY_UNVALIDATED,
            _CANCEL_REPLY_TESTED,
            True,
            "untested",
            id="cancel-untested",
        ),
        pytest.param(
            _build_cancel_exit_result,
            _CANCEL_REPLY_DEFAULT,
            _CANCEL_REPLY_UNVALIDATED,
            _CANCEL_REPLY_TESTED,
            True,
            "passing_test",
            id="cancel-passing_test",
        ),
        pytest.param(
            _build_cancel_exit_result,
            _CANCEL_REPLY_DEFAULT,
            _CANCEL_REPLY_UNVALIDATED,
            _CANCEL_REPLY_TESTED,
            True,
            "failed_test",
            id="cancel-failed_test",
        ),
    ],
)
def test_capacity_exit_state_disposition(
    builder,
    default_reply: str,
    unvalidated_reply: str,
    tested_reply: str,
    expected_cancelled: bool,
    state_kind: str,
) -> None:
    ctx, wf = _state_ctx(state_kind)

    result = builder(ctx, global_llm_context=None)

    disposition, reply_key, surfaces = _STATE_EXPECTATIONS[state_kind]
    expected_reply = {"default": default_reply, "unvalidated": unvalidated_reply, "tested": tested_reply}[reply_key]

    assert result.proposal_disposition == disposition
    assert result.user_response == expected_reply
    assert result.cancelled is expected_cancelled
    if surfaces:
        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
    else:
        assert result.updated_workflow is None
        assert result.workflow_yaml is None


class TestBuildTimeoutExitResult:
    def test_missing_yaml_drops_untested_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml=None, last_test_ok=None)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _TIMEOUT_REPLY_DEFAULT

    def test_missing_yaml_drops_tested_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml=None, last_test_ok=True)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _TIMEOUT_REPLY_DEFAULT

    def test_verified_terminal_state_preserves_tested_proposal_on_timeout(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="version: '1.0'",
            last_test_ok=None,
            last_test_suspicious_success=True,
        )
        ctx.verified_terminal_proposal_ready = True
        ctx.completion_verification_result = CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["c0"],
            verdicts=[CriterionVerdict(criterion_id="c0", state="satisfied", reason_code="evidence_confirms")],
        )

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _TIMEOUT_REPLY_TESTED
        assert result.clear_proposed_workflow is False

    def test_stale_latch_without_judge_verdict_does_not_preserve_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="version: '1.0'",
            last_test_ok=None,
            last_test_suspicious_success=True,
        )
        ctx.verified_terminal_proposal_ready = True
        ctx.completion_verification_result = None
        ctx.last_artifact_health_blocker_reason = None

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.user_response == _TIMEOUT_REPLY_DEFAULT

    def test_suspicious_current_run_drops_last_good_workflow_without_verified_terminal_state(self) -> None:
        wf = MagicMock(name="wf")
        last_good = MagicMock(name="last_good")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="version: 'broken'",
            last_test_ok=None,
            last_test_suspicious_success=True,
            last_good_workflow=last_good,
            last_good_workflow_yaml="version: 'tested'",
        )

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _TIMEOUT_REPLY_DEFAULT


class TestBuildCancelledExitResult:
    def test_total_timeout_latch_routes_cancel_to_timeout_wip(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)
        ctx.copilot_total_timeout_exceeded = True

        result = _build_cancelled_exit_result(ctx, global_llm_context=None)

        assert result.cancelled is False
        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.proposal_disposition == "review_untested"
        assert result.user_response == _TIMEOUT_REPLY_UNVALIDATED

    def test_regular_cancel_uses_cancel_wip_path(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)

        result = _build_cancelled_exit_result(ctx, global_llm_context=None)

        assert result.cancelled is True
        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.proposal_disposition == "review_untested"
        assert result.user_response == _CANCEL_REPLY_UNVALIDATED


class TestBuildUnexpectedErrorExitResult:
    def test_no_workflow_falls_back_to_default_reply(self) -> None:
        ctx = _ctx(last_workflow=None, last_workflow_yaml=None, last_test_ok=None)

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.proposal_disposition == "auto_applicable"
        assert "An unexpected error occurred. Please try again." not in result.user_response
        assert "Copilot hit an internal error before it could finish this turn" in result.user_response
        assert "The workflow was not modified" in result.user_response
        assert "reference cpe_" in result.user_response

    def test_untested_workflow_surfaces_as_unvalidated_wip(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.proposal_disposition == "review_untested"
        assert result.user_response == _UNEXPECTED_ERROR_REPLY_UNVALIDATED

    def test_passing_test_surfaces_as_tested_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=True)

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.proposal_disposition == "review_tested"
        assert result.user_response == _UNEXPECTED_ERROR_REPLY_TESTED

    def test_failed_test_drops_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=False)

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.proposal_disposition == "auto_applicable"
        assert "Copilot hit an internal error before it could finish this turn" in result.user_response
        assert "The workflow was preserved" in result.user_response
        assert "reference cpe_" in result.user_response

    def test_failed_test_uses_recorded_blocker_reply(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=False)
        ctx.last_update_block_count = 3
        ctx.latest_diagnosis_repair_contract = _blocker_contract(
            "Browser session was no longer reachable.",
            workflow_run_id="wr_test",
        )

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.user_response == (
            "I built a 3-block draft and tested it, but the test couldn't finish: "
            "Browser session was no longer reachable. Last run status: running."
        )

    def test_failed_test_scrubs_recorded_internal_tool_instruction(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=False)
        ctx.last_update_block_count = 3
        ctx.latest_diagnosis_repair_contract = _blocker_contract(
            "Less than 90 seconds remain in this Copilot turn after the previous workflow run failed. "
            "Do NOT retry block-running tools. Use only existing run evidence and quick browser inspection.",
            run_status="canceled",
        )

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.user_response.startswith(
            "I built a 3-block draft and was still testing it when the turn ran out of time."
        )
        assert "the test failed" not in result.user_response
        assert "Do NOT" not in result.user_response
        assert "block-running tools" not in result.user_response

    def test_aborted_test_surfaces_unvalidated_draft_with_recorded_blocker_reply(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)
        ctx.test_after_update_done = True
        ctx.last_update_block_count = 4
        ctx.latest_diagnosis_repair_contract = _blocker_contract(
            "The browser session disappeared before screenshot verification could complete.",
            run_status="aborted",
            workflow_run_id="wr_test",
        )

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.proposal_disposition == "review_untested"
        assert result.user_response == (
            "I built a 4-block draft and tested it, but the test couldn't finish: "
            "The browser session disappeared before screenshot verification could complete. "
            "Last run status: aborted."
        )

    def test_browser_only_blocker_does_not_claim_tested_and_redacts_internal_details(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)
        ctx.test_after_update_done = False
        ctx.last_update_block_count = 2
        ctx.latest_diagnosis_repair_contract = _blocker_contract(
            "Browser session pbs_123456 not found while reading https://example.test/path?token=secret.",
            run_status="aborted",
        )

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.proposal_disposition == "review_untested"
        assert result.user_response == (
            "I built a 2-block draft, but I couldn't start a test run: "
            "Browser session not found while reading https://example.test. Last run status: aborted."
        )
        assert "pbs_" not in result.user_response
        assert "token=secret" not in result.user_response

    def test_sandbox_refusal_does_not_claim_a_test_ran(self) -> None:
        """No run row means nothing executed, so the reply must not say it tested the draft."""
        from skyvern.forge.sdk.copilot.diagnosis_repair_contract import build_diagnosis_repair_contract
        from skyvern.forge.sdk.copilot.tools.run_execution import _copilot_sandbox_unavailable_result

        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=False)
        ctx.test_after_update_done = True
        ctx.last_update_block_count = 6
        ctx.latest_diagnosis_repair_contract = build_diagnosis_repair_contract(
            source_tool="run_blocks_and_collect_debug",
            result=_copilot_sandbox_unavailable_result(),
            ctx=ctx,
        )

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert "tested it" not in result.user_response
        assert "I couldn't start a test run" in result.user_response

    def test_suspicious_success_drops_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="version: '1.0'",
            last_test_ok=None,
            last_test_suspicious_success=True,
        )

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.proposal_disposition == "auto_applicable"
        assert "Copilot hit an internal error before it could finish this turn" in result.user_response
        assert "The workflow was preserved" in result.user_response
        assert "reference cpe_" in result.user_response


class TestWipExitSurfacesLastGoodWithForceReviewNotUnvalidated:
    """Mid-flight overwrite branch offers ``last_good_workflow`` with ``force_review=True, unvalidated=False``."""

    @pytest.mark.parametrize(
        ("builder", "tested_reply", "last_test_ok", "expected_cancelled"),
        [
            pytest.param(_build_cancel_exit_result, _CANCEL_REPLY_TESTED, None, True, id="cancel"),
            pytest.param(_build_timeout_exit_result, _TIMEOUT_REPLY_TESTED, False, False, id="timeout-failed-test"),
            pytest.param(_build_max_turns_exit_result, _MAX_TURNS_REPLY_TESTED, None, False, id="max_turns"),
            pytest.param(
                _build_unexpected_error_exit_result,
                _UNEXPECTED_ERROR_REPLY_TESTED,
                None,
                False,
                id="unexpected_error",
            ),
        ],
    )
    def test_overwrite_surfaces_last_good_as_tested_force_review(
        self,
        builder,
        tested_reply: str,
        last_test_ok: bool | None,
        expected_cancelled: bool,
    ) -> None:
        ctx = _overwrite_ctx(last_test_ok=last_test_ok)

        result = builder(ctx, global_llm_context=None)

        assert result.updated_workflow is ctx.last_good_workflow
        assert result.workflow_yaml == "version: good"
        assert result.proposal_disposition == "review_tested"
        assert result.user_response == tested_reply
        assert result.cancelled is expected_cancelled

    def test_unexpected_error_with_overwrite_and_blocker_describes_latest_attempt_separately(self) -> None:
        ctx = _overwrite_ctx(last_test_ok=None)
        ctx.last_update_block_count = 5
        ctx.latest_diagnosis_repair_contract = _blocker_contract(
            "Browser session pbs_789 not found during screenshot verification.",
            run_status="aborted",
        )

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is ctx.last_good_workflow
        assert result.proposal_disposition == "review_tested"
        assert result.user_response == (
            f"{_UNEXPECTED_ERROR_REPLY_TESTED} "
            "The latest attempted change did not verify: "
            "Browser session not found during screenshot verification. Last run status: aborted."
        )
        assert "pbs_" not in result.user_response

    def test_cancelled_total_timeout_latch_uses_force_review_not_unvalidated(self) -> None:
        ctx = _overwrite_ctx(last_test_ok=None)
        ctx.copilot_total_timeout_exceeded = True

        result = _build_cancelled_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is ctx.last_good_workflow
        assert result.proposal_disposition == "review_tested"
        assert result.cancelled is False
        assert result.user_response == _TIMEOUT_REPLY_TESTED


class TestTimeoutExitNamesTimeAndDraftState:
    def test_untested_draft_names_time_and_that_it_is_unverified(self) -> None:
        ctx = _ctx(last_workflow=MagicMock(name="wf"), last_workflow_yaml="version: '1.0'", last_test_ok=None)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.proposal_disposition == "review_untested"
        assert "ran out of time" in result.user_response
        assert "hasn't been verified end-to-end" in result.user_response

    def test_tested_draft_names_time_and_that_it_is_tested(self) -> None:
        ctx = _ctx(last_workflow=MagicMock(name="wf"), last_workflow_yaml="version: '1.0'", last_test_ok=True)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.proposal_disposition == "review_tested"
        assert "ran out of time" in result.user_response
        assert "tested draft" in result.user_response

    def test_no_draft_still_names_time(self) -> None:
        ctx = _ctx(last_workflow=None, last_workflow_yaml=None, last_test_ok=None)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.proposal_disposition == "auto_applicable"
        assert "ran out of time" in result.user_response

    def test_untested_draft_with_recorded_failure_still_names_time(self) -> None:
        # The common shape: the failed test run is what spent the budget, so a
        # recorded failure is present on almost every real deadline expiry.
        ctx = _ctx(last_workflow=MagicMock(name="wf"), last_workflow_yaml="version: '1.0'", last_test_ok=False)
        ctx.copilot_total_timeout_exceeded = True
        ctx.last_update_block_count = 1
        ctx.latest_diagnosis_repair_contract = _blocker_contract(
            "could not convert string to float", run_status="failed"
        )

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert "ran out of time" in result.user_response
        assert "could not convert string to float" in result.user_response
        assert not result.user_response.startswith("I built")

    def test_no_draft_with_recorded_failure_still_names_time(self) -> None:
        ctx = _ctx(last_workflow=None, last_workflow_yaml=None, last_test_ok=False)
        ctx.copilot_total_timeout_exceeded = True
        ctx.latest_diagnosis_repair_contract = _blocker_contract(
            "could not convert string to float", run_status="failed"
        )

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert "ran out of time" in result.user_response
        assert "could not convert string to float" in result.user_response

    def test_max_turns_exit_keeps_the_recorded_failure_reply(self) -> None:
        # The deadline precedence must not leak into sibling capacity exits.
        ctx = _ctx(last_workflow=MagicMock(name="wf"), last_workflow_yaml="version: '1.0'", last_test_ok=False)
        ctx.last_update_block_count = 1
        ctx.latest_diagnosis_repair_contract = _blocker_contract(
            "could not convert string to float", run_status="failed"
        )

        result = _build_max_turns_exit_result(ctx, global_llm_context=None)

        assert "ran out of time" not in result.user_response
        assert result.user_response.startswith("I built")

    def test_held_draft_shape_names_time_draft_state_and_keeps_evidence(self) -> None:
        # The shape both live deadline runs produced: review_untested / proposal_pending.
        ctx = _ctx(last_workflow=MagicMock(name="wf"), last_workflow_yaml="version: '1.0'", last_test_ok=None)
        ctx.copilot_total_timeout_exceeded = True
        ctx.last_update_block_count = 1
        ctx.latest_diagnosis_repair_contract = _blocker_contract("Run completed", run_status="completed")

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.proposal_disposition == "review_untested"
        assert "ran out of time" in result.user_response
        assert "hasn't been verified end-to-end" in result.user_response
        assert "Run completed" in result.user_response

    def test_interrupted_run_is_not_reported_as_a_failed_verification(self) -> None:
        # A budget-paced halt was interrupted, not disproven. The deadline copy already says the
        # work is unverified, so appending "the last test did not verify" would mis-attribute twice.
        ctx = _ctx(last_workflow=MagicMock(name="wf"), last_workflow_yaml="version: '1.0'", last_test_ok=False)
        ctx.copilot_total_timeout_exceeded = True
        ctx.last_update_block_count = 3
        ctx.latest_diagnosis_repair_contract = _blocker_contract(
            "Less than 90 seconds remain in this Copilot turn after the previous workflow run failed. "
            "Do NOT retry block-running tools. Use only existing run evidence and quick browser inspection.",
            run_status="canceled",
        )

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert "ran out of time" in result.user_response
        assert "did not verify" not in result.user_response
        assert "Do NOT" not in result.user_response

    def test_last_good_branch_also_drops_the_trailing_verdict_when_interrupted(self) -> None:
        # wip_last_good_workflow composes its own trailing sentence; the interrupted-run
        # exemption has to reach that branch too, not just the two above it.
        ctx = _overwrite_ctx(last_test_ok=False)
        ctx.copilot_total_timeout_exceeded = True
        ctx.last_update_block_count = 2
        ctx.latest_diagnosis_repair_contract = _blocker_contract(
            "Less than 90 seconds remain in this Copilot turn after the previous workflow run failed. "
            "Do NOT retry block-running tools.",
            run_status="canceled",
        )

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert "ran out of time" in result.user_response
        assert "did not verify" not in result.user_response

"""Tests for the capacity-exhausted WIP carve-outs (timeout, max-turns)."""

from __future__ import annotations

from unittest.mock import MagicMock

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


def _blocker_contract(reason: str, *, run_status: str | None = "running") -> DiagnosisRepairContract:
    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(source_tool="get_browser_screenshot", run_status=run_status),
        diagnosis_result=DiagnosisResult(root_cause_summary=reason, confidence=0.9),
        repair_decision=RepairDecision(next_action="stop"),
        verification_result=VerificationResult(
            run_status=run_status,
            remaining_blocker=reason,
        ),
    )


class TestBuildTimeoutExitResult:
    def test_no_workflow_falls_back_to_default_reply(self) -> None:
        ctx = _ctx(last_workflow=None, last_workflow_yaml=None, last_test_ok=None)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _TIMEOUT_REPLY_DEFAULT

    def test_untested_workflow_surfaces_as_unvalidated_wip(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.proposal_disposition == "review_untested"
        assert result.user_response == _TIMEOUT_REPLY_UNVALIDATED

    def test_failed_test_drops_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=False)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _TIMEOUT_REPLY_DEFAULT

    def test_passing_test_surfaces_as_tested_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=True)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _TIMEOUT_REPLY_TESTED

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

    def test_suspicious_success_drops_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="version: '1.0'",
            last_test_ok=None,
            last_test_suspicious_success=True,
        )

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _TIMEOUT_REPLY_DEFAULT


class TestBuildMaxTurnsExitResult:
    def test_no_workflow_falls_back_to_default_reply(self) -> None:
        ctx = _ctx(last_workflow=None, last_workflow_yaml=None, last_test_ok=None)

        result = _build_max_turns_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _MAX_TURNS_REPLY_DEFAULT

    def test_untested_workflow_surfaces_as_unvalidated_wip(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)

        result = _build_max_turns_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.proposal_disposition == "review_untested"
        assert result.user_response == _MAX_TURNS_REPLY_UNVALIDATED

    def test_passing_test_surfaces_as_tested_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=True)

        result = _build_max_turns_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _MAX_TURNS_REPLY_TESTED

    def test_failed_test_drops_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=False)

        result = _build_max_turns_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _MAX_TURNS_REPLY_DEFAULT

    def test_suspicious_success_drops_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(
            last_workflow=wf,
            last_workflow_yaml="version: '1.0'",
            last_test_ok=None,
            last_test_suspicious_success=True,
        )

        result = _build_max_turns_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _MAX_TURNS_REPLY_DEFAULT


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
        assert result.proposal_disposition == "auto_applicable"
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
        ctx.latest_diagnosis_repair_contract = _blocker_contract("Browser session was no longer reachable.")

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.user_response == (
            "I built a 3-block draft and tested it, but the test couldn't finish: "
            "Browser session was no longer reachable. Last run status: running."
        )

    def test_aborted_test_surfaces_unvalidated_draft_with_recorded_blocker_reply(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)
        ctx.test_after_update_done = True
        ctx.last_update_block_count = 4
        ctx.latest_diagnosis_repair_contract = _blocker_contract(
            "The browser session disappeared before screenshot verification could complete.",
            run_status="aborted",
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
            "I built a 2-block draft, but I couldn't verify it: "
            "Browser session not found while reading https://example.test. Last run status: aborted."
        )
        assert "pbs_" not in result.user_response
        assert "token=secret" not in result.user_response

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


class TestBuildCancelExitResult:
    def test_no_workflow_falls_back_to_cancel_reply(self) -> None:
        ctx = _ctx(last_workflow=None, last_workflow_yaml=None, last_test_ok=None)

        result = _build_cancel_exit_result(ctx, global_llm_context=None)

        assert result.cancelled is True
        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _CANCEL_REPLY_DEFAULT

    def test_untested_workflow_surfaces_as_unvalidated_cancel_wip(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)

        result = _build_cancel_exit_result(ctx, global_llm_context=None)

        assert result.cancelled is True
        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.proposal_disposition == "review_untested"
        assert result.user_response == _CANCEL_REPLY_UNVALIDATED

    def test_passing_test_surfaces_as_tested_cancel_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=True)

        result = _build_cancel_exit_result(ctx, global_llm_context=None)

        assert result.cancelled is True
        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _CANCEL_REPLY_TESTED

    def test_failed_test_drops_cancel_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=False)

        result = _build_cancel_exit_result(ctx, global_llm_context=None)

        assert result.cancelled is True
        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.proposal_disposition == "auto_applicable"
        assert result.user_response == _CANCEL_REPLY_DEFAULT


class TestWipExitSurfacesLastGoodWithForceReviewNotUnvalidated:
    """Mid-flight overwrite branch offers ``last_good_workflow`` with ``force_review=True, unvalidated=False``."""

    def _overwrite_ctx(self, *, last_test_ok: bool | None) -> MagicMock:
        good = MagicMock(name="wf-good")
        in_flight = MagicMock(name="wf-in-flight")
        return _ctx(
            last_workflow=in_flight,
            last_workflow_yaml="version: in-flight",
            last_test_ok=last_test_ok,
            last_good_workflow=good,
            last_good_workflow_yaml="version: good",
        )

    def test_cancel_with_overwrite_surfaces_last_good_as_tested_force_review(self) -> None:
        ctx = self._overwrite_ctx(last_test_ok=None)

        result = _build_cancel_exit_result(ctx, global_llm_context=None)

        assert result.cancelled is True
        assert result.updated_workflow is ctx.last_good_workflow
        assert result.workflow_yaml == "version: good"
        assert result.proposal_disposition == "review_tested"
        assert result.user_response == _CANCEL_REPLY_TESTED

    def test_timeout_with_overwrite_surfaces_last_good_as_tested_force_review(self) -> None:
        ctx = self._overwrite_ctx(last_test_ok=False)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is ctx.last_good_workflow
        assert result.proposal_disposition == "review_tested"
        assert result.user_response == _TIMEOUT_REPLY_TESTED

    def test_max_turns_with_overwrite_surfaces_last_good_as_tested_force_review(self) -> None:
        ctx = self._overwrite_ctx(last_test_ok=None)

        result = _build_max_turns_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is ctx.last_good_workflow
        assert result.proposal_disposition == "review_tested"
        assert result.user_response == _MAX_TURNS_REPLY_TESTED

    def test_unexpected_error_with_overwrite_surfaces_last_good_as_tested_force_review(self) -> None:
        ctx = self._overwrite_ctx(last_test_ok=None)

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is ctx.last_good_workflow
        assert result.proposal_disposition == "review_tested"
        assert result.user_response == _UNEXPECTED_ERROR_REPLY_TESTED

    def test_unexpected_error_with_overwrite_and_blocker_describes_latest_attempt_separately(self) -> None:
        ctx = self._overwrite_ctx(last_test_ok=None)
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
        ctx = self._overwrite_ctx(last_test_ok=None)
        ctx.copilot_total_timeout_exceeded = True

        result = _build_cancelled_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is ctx.last_good_workflow
        assert result.proposal_disposition == "review_tested"
        assert result.cancelled is False
        assert result.user_response == _TIMEOUT_REPLY_TESTED

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
    _UNEXPECTED_ERROR_REPLY_DEFAULT,
    _UNEXPECTED_ERROR_REPLY_TESTED,
    _UNEXPECTED_ERROR_REPLY_UNVALIDATED,
    _build_cancel_exit_result,
    _build_cancelled_exit_result,
    _build_max_turns_exit_result,
    _build_timeout_exit_result,
    _build_unexpected_error_exit_result,
)


def _ctx(
    *,
    last_workflow: object | None,
    last_workflow_yaml: str | None,
    last_test_ok: bool | None,
    last_test_suspicious_success: bool = False,
) -> MagicMock:
    ctx = MagicMock()
    ctx.last_workflow = last_workflow
    ctx.last_workflow_yaml = last_workflow_yaml
    ctx.last_test_ok = last_test_ok
    ctx.last_test_suspicious_success = last_test_suspicious_success
    ctx.copilot_total_timeout_exceeded = False
    ctx.workflow_persisted = last_workflow is not None
    ctx.total_tokens_used = None
    return ctx


class TestBuildTimeoutExitResult:
    def test_no_workflow_falls_back_to_default_reply(self) -> None:
        ctx = _ctx(last_workflow=None, last_workflow_yaml=None, last_test_ok=None)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.unvalidated is False
        assert result.user_response == _TIMEOUT_REPLY_DEFAULT

    def test_untested_workflow_surfaces_as_unvalidated_wip(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.unvalidated is True
        assert result.user_response == _TIMEOUT_REPLY_UNVALIDATED

    def test_failed_test_drops_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=False)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.unvalidated is False
        assert result.user_response == _TIMEOUT_REPLY_DEFAULT

    def test_passing_test_surfaces_as_tested_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=True)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.unvalidated is False
        assert result.user_response == _TIMEOUT_REPLY_TESTED

    def test_missing_yaml_drops_untested_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml=None, last_test_ok=None)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.unvalidated is False
        assert result.user_response == _TIMEOUT_REPLY_DEFAULT

    def test_missing_yaml_drops_tested_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml=None, last_test_ok=True)

        result = _build_timeout_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.unvalidated is False
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
        assert result.unvalidated is False
        assert result.user_response == _TIMEOUT_REPLY_DEFAULT


class TestBuildMaxTurnsExitResult:
    def test_no_workflow_falls_back_to_default_reply(self) -> None:
        ctx = _ctx(last_workflow=None, last_workflow_yaml=None, last_test_ok=None)

        result = _build_max_turns_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.unvalidated is False
        assert result.user_response == _MAX_TURNS_REPLY_DEFAULT

    def test_untested_workflow_surfaces_as_unvalidated_wip(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)

        result = _build_max_turns_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.unvalidated is True
        assert result.user_response == _MAX_TURNS_REPLY_UNVALIDATED

    def test_passing_test_surfaces_as_tested_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=True)

        result = _build_max_turns_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.unvalidated is False
        assert result.user_response == _MAX_TURNS_REPLY_TESTED

    def test_failed_test_drops_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=False)

        result = _build_max_turns_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.unvalidated is False
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
        assert result.unvalidated is False
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
        assert result.unvalidated is True
        assert result.user_response == _TIMEOUT_REPLY_UNVALIDATED

    def test_regular_cancel_uses_cancel_wip_path(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)

        result = _build_cancelled_exit_result(ctx, global_llm_context=None)

        assert result.cancelled is True
        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.unvalidated is True
        assert result.user_response == _CANCEL_REPLY_UNVALIDATED


class TestBuildUnexpectedErrorExitResult:
    def test_no_workflow_falls_back_to_default_reply(self) -> None:
        ctx = _ctx(last_workflow=None, last_workflow_yaml=None, last_test_ok=None)

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.unvalidated is False
        assert result.user_response == _UNEXPECTED_ERROR_REPLY_DEFAULT

    def test_untested_workflow_surfaces_as_unvalidated_wip(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.unvalidated is True
        assert result.user_response == _UNEXPECTED_ERROR_REPLY_UNVALIDATED

    def test_passing_test_surfaces_as_tested_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=True)

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.unvalidated is False
        assert result.user_response == _UNEXPECTED_ERROR_REPLY_TESTED

    def test_failed_test_drops_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=False)

        result = _build_unexpected_error_exit_result(ctx, global_llm_context=None)

        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.unvalidated is False
        assert result.user_response == _UNEXPECTED_ERROR_REPLY_DEFAULT

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
        assert result.unvalidated is False
        assert result.user_response == _UNEXPECTED_ERROR_REPLY_DEFAULT


class TestBuildCancelExitResult:
    def test_no_workflow_falls_back_to_cancel_reply(self) -> None:
        ctx = _ctx(last_workflow=None, last_workflow_yaml=None, last_test_ok=None)

        result = _build_cancel_exit_result(ctx, global_llm_context=None)

        assert result.cancelled is True
        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.unvalidated is False
        assert result.user_response == _CANCEL_REPLY_DEFAULT

    def test_untested_workflow_surfaces_as_unvalidated_cancel_wip(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=None)

        result = _build_cancel_exit_result(ctx, global_llm_context=None)

        assert result.cancelled is True
        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.unvalidated is True
        assert result.user_response == _CANCEL_REPLY_UNVALIDATED

    def test_passing_test_surfaces_as_tested_cancel_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=True)

        result = _build_cancel_exit_result(ctx, global_llm_context=None)

        assert result.cancelled is True
        assert result.updated_workflow is wf
        assert result.workflow_yaml == "version: '1.0'"
        assert result.unvalidated is False
        assert result.user_response == _CANCEL_REPLY_TESTED

    def test_failed_test_drops_cancel_proposal(self) -> None:
        wf = MagicMock(name="wf")
        ctx = _ctx(last_workflow=wf, last_workflow_yaml="version: '1.0'", last_test_ok=False)

        result = _build_cancel_exit_result(ctx, global_llm_context=None)

        assert result.cancelled is True
        assert result.updated_workflow is None
        assert result.workflow_yaml is None
        assert result.unvalidated is False
        assert result.user_response == _CANCEL_REPLY_DEFAULT

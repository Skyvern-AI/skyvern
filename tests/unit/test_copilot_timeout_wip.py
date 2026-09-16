"""Regression coverage for non-budget terminal copy retained after budget drain."""

from __future__ import annotations

from skyvern.forge.sdk.copilot.agent import (
    _UNEXPECTED_ERROR_REPLY_UNVALIDATED,
    _build_cancel_exit_result,
    _build_max_turns_exit_result,
    _build_timeout_exit_result,
)
from tests.unit.copilot_test_helpers import make_copilot_ctx


def test_budget_exit_builders_persist_typed_no_report() -> None:
    for builder, source, terminal_reason in (
        (_build_timeout_exit_result, "deadline", "timeout"),
        (_build_max_turns_exit_result, "max_turns", "max_turns"),
    ):
        ctx = make_copilot_ctx()
        result = builder(ctx, global_llm_context=None)

        assert result.user_response == ""
        assert result.turn_outcome is not None
        assert result.turn_outcome.budget_expired is True
        assert result.turn_outcome.budget_expiry_source == source
        assert result.turn_outcome.budget_expiry_report_produced is False
        assert result.turn_outcome.terminal_reason == terminal_reason


def test_cancel_exit_writes_no_copy_of_its_own() -> None:
    ctx = make_copilot_ctx()
    result = _build_cancel_exit_result(ctx, global_llm_context=None)

    assert result.user_response == ""
    assert result.turn_outcome is not None
    assert result.turn_outcome.terminal_reason == "cancel"


def test_unexpected_error_draft_copy_remains_available() -> None:
    assert "unexpected issue" in _UNEXPECTED_ERROR_REPLY_UNVALIDATED

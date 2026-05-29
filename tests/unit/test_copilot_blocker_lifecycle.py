from __future__ import annotations

from types import SimpleNamespace

from skyvern.forge.sdk.copilot.blocker_signal import BlockerKind, CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.loop_detection import record_tool_step_result_for_ctx


def _ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="org",
        workflow_id="wf",
        workflow_permanent_id="wfp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )


def _signal(
    *,
    kind: BlockerKind = "authority_denied",
    cleared_by: frozenset[str] = frozenset(),
    reason: str = "some_reason",
) -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind=kind,
        agent_steering_text="steering",
        user_facing_reason="I couldn't do that on this turn.",
        recovery_hint="retry_with_different_tool",
        cleared_by_tools=cleared_by,
        internal_reason_code=reason,
        blocked_tool="update_and_run_blocks",
    )


def test_recoverable_blocker_clears_on_matching_tool_success() -> None:
    ctx = _ctx()
    ctx.blocker_signal = _signal(cleared_by=frozenset({"update_workflow"}))
    record_tool_step_result_for_ctx(ctx, "update_workflow", {"workflow_yaml": "y"}, {"ok": True})
    assert ctx.blocker_signal is None


def test_recoverable_blocker_does_not_clear_on_unrelated_tool_success() -> None:
    ctx = _ctx()
    signal = _signal(cleared_by=frozenset({"update_workflow"}))
    ctx.blocker_signal = signal
    record_tool_step_result_for_ctx(ctx, "list_credentials", None, {"ok": True})
    assert ctx.blocker_signal is signal


def test_loop_blocker_clears_on_progress_tool_success() -> None:
    ctx = _ctx()
    signal = _signal(kind="loop_detected", cleared_by=frozenset(), reason="loop_detected_generic")
    ctx.blocker_signal = signal
    record_tool_step_result_for_ctx(ctx, "update_workflow", {"workflow_yaml": "y"}, {"ok": True})
    assert ctx.blocker_signal is None


def test_terminal_tool_error_stays_sticky_on_any_success() -> None:
    ctx = _ctx()
    signal = _signal(kind="tool_error", cleared_by=frozenset(), reason="tool_error_repeated_action_abort")
    ctx.blocker_signal = signal
    record_tool_step_result_for_ctx(ctx, "update_workflow", {"workflow_yaml": "y"}, {"ok": True})
    assert ctx.blocker_signal is signal


def test_failed_dispatch_does_not_clear_signal() -> None:
    ctx = _ctx()
    signal = _signal(cleared_by=frozenset({"update_workflow"}))
    ctx.blocker_signal = signal
    record_tool_step_result_for_ctx(ctx, "update_workflow", {"workflow_yaml": "y"}, {"ok": False, "error": "x"})
    # Failed dispatch must not satisfy a recovery hint.
    assert ctx.blocker_signal is signal


def test_per_tool_budget_blocker_clears_on_update_and_run_blocks_success() -> None:
    """Per-tool-budget steering directs the agent to split blocks; the recovery
    can land via either ``update_workflow`` or ``update_and_run_blocks``."""
    from skyvern.forge.sdk.copilot.tools import _per_tool_budget_problem_rerun_signal

    ctx = _ctx()
    ctx.per_tool_budget_problem_block_labels = ["heavy_navigation"]
    signal = _per_tool_budget_problem_rerun_signal(ctx, None, "run_blocks_and_collect_debug")
    assert signal is not None
    assert "update_and_run_blocks" in signal.cleared_by_tools
    ctx.blocker_signal = signal
    record_tool_step_result_for_ctx(ctx, "update_and_run_blocks", {"workflow_yaml": "y"}, {"ok": True})
    assert ctx.blocker_signal is None


def test_reconciliation_canceled_status_replaces_no_input_signal_with_requires_input() -> None:
    """When the reconciliation read resolves the pending run as canceled, the
    'I'll check what happened' blocker is replaced with one that asks the
    user to decide."""
    from skyvern.forge.sdk.copilot.tools import _maybe_clear_reconciliation_flag
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

    ctx = _ctx()
    ctx.pending_reconciliation_run_id = "wr_pending"
    ctx.blocker_signal = _signal(
        kind="tool_error",
        cleared_by=frozenset(),
        reason="tool_error_pending_reconciliation_no_input",
    )
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_pending",
            "overall_status": WorkflowRunStatus.canceled.value,
        },
    }
    _maybe_clear_reconciliation_flag(ctx, result)
    assert ctx.pending_reconciliation_requires_user_input is True
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_pending_reconciliation_requires_input"
    assert ctx.blocker_signal.recovery_hint == "ask_user_clarifying"


def test_reconciliation_canceled_does_not_overwrite_unrelated_blocker() -> None:
    """If ctx already holds an unrelated blocker (e.g. ``loop_detected``), the
    canceled-status transition must not silently replace it. The no_input
    clear is a no-op against the unrelated reason code, and the subsequent
    ``isinstance`` check sees the blocker still set and skips the set."""
    from skyvern.forge.sdk.copilot.tools import _maybe_clear_reconciliation_flag
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

    ctx = _ctx()
    ctx.pending_reconciliation_run_id = "wr_pending"
    unrelated = _signal(
        kind="loop_detected",
        cleared_by=frozenset(),
        reason="loop_detected_generic",
    )
    ctx.blocker_signal = unrelated
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_pending",
            "overall_status": WorkflowRunStatus.canceled.value,
        },
    }
    _maybe_clear_reconciliation_flag(ctx, result)
    assert ctx.pending_reconciliation_requires_user_input is True
    assert ctx.blocker_signal is unrelated


def test_reconciliation_signal_steering_text_does_not_leak_through_user_facing_check() -> None:
    """The reconciliation requires-input signal contains 'do not run' in its
    ``agent_steering_text`` (legitimate agent imperative). If a future
    refactor accidentally swaps the field used by the renderer, the user
    would see leaky agent-control prose. Test that
    ``assert_clean_user_facing_text`` correctly rejects the steering text so
    such a swap would fail tests immediately."""
    import pytest

    from skyvern.forge.sdk.copilot.blocker_signal import assert_clean_user_facing_text
    from skyvern.forge.sdk.copilot.tools import _maybe_clear_reconciliation_flag
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

    ctx = _ctx()
    ctx.pending_reconciliation_run_id = "wr_pending"
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_pending",
            "overall_status": WorkflowRunStatus.canceled.value,
        },
    }
    _maybe_clear_reconciliation_flag(ctx, result)
    assert ctx.blocker_signal is not None
    # user_facing_reason must pass the deny list.
    assert_clean_user_facing_text(ctx.blocker_signal.user_facing_reason)
    # agent_steering_text must FAIL the deny list — that's the contract:
    # steering text is for the LLM, not the user, and may carry imperatives.
    # If a future refactor pipes steering text into the renderer, this fails.
    with pytest.raises(ValueError):
        assert_clean_user_facing_text(ctx.blocker_signal.agent_steering_text)

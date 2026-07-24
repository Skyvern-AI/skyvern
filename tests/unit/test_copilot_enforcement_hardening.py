"""Tests for enforcement hardening landed in copilot-stack/06b:

* fresh ``CopilotContext`` flows through ``_check_enforcement`` without raising
  AttributeError (enforcement fields have dataclass defaults).
* ``_prune_input_list`` compacts the ``arguments`` field of older tool calls
  so large payloads (like a full workflow YAML) don't accumulate.
* ``_check_enforcement`` does NOT clear ``last_test_suspicious_success`` after
  emitting the nudge — if the agent ignores it and replies again, the nudge
  must re-fire.
* ``_recover_from_context_overflow`` strips image payloads out of the current
  turn input so a freshly injected screenshot doesn't re-trigger overflow.
* ``streaming_adapter._update_enforcement_from_tool`` resets the
  ``navigate_enforcement_done`` latch on each new ``navigate_browser`` call
  so the nudge fires on every navigate-without-observe, not only the first.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.agent import (
    _finalize_result_with_blocker_override,
    _with_scouted_spine_missing_steps,
)
from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
    clear_terminal_evidence_on_workflow_edit,
    maybe_clear_blocker_signal_on_tool_success,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import UNFORGIVEN_DROP_FINDING, ObligationFinding
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import AgentResult, CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisInput,
    DiagnosisRepairContract,
    DiagnosisResult,
    RepairDecision,
    RepairLoopState,
    VerificationResult,
)
from skyvern.forge.sdk.copilot.enforcement import (
    MAX_CODE_AUTHORING_GUARDRAIL_REJECTS,
    MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS,
    MAX_NO_PROGRESS_INTERACTION_ATTEMPTS,
    MAX_PROBABLE_SITE_BLOCK_STOP_NUDGES,
    POST_FAILED_TEST_INSPECT_FIRST_NUDGE,
    POST_FAILED_TEST_NUDGE,
    POST_NAVIGATE_NUDGE,
    POST_PER_TOOL_BUDGET_NUDGE,
    POST_PER_TOOL_BUDGET_STOP_NUDGE,
    POST_SUSPICIOUS_SUCCESS_NUDGE,
    PROBABLE_SITE_BLOCK_STREAK_STOP_AT,
    SCREENSHOT_PLACEHOLDER,
    CopilotNonRetriableNavError,
    _check_enforcement,
    _is_context_window_error,
    _maybe_raise_non_retriable_nav,
    _needs_inspect_before_repair_nudge,
    _prune_input_list,
    _record_code_authoring_guardrail_reject,
    _recover_from_context_overflow,
    _scouted_spine_missing_text,
    _strip_input_images,
    register_no_progress_interaction_click,
    reset_no_progress_interaction_count,
    synthesized_trajectory_reaches_goal,
)
from skyvern.forge.sdk.copilot.output_contracts import OutputContractAdvisoryState
from skyvern.forge.sdk.copilot.run_outcome import TERMINAL_CHALLENGE_BLOCKER_REASON_CODE
from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool
from skyvern.forge.sdk.copilot.tools.workflow_update import _pre_persist_scouted_spine_result
from skyvern.forge.sdk.copilot.turn_halt import (
    ADVISORY_DISPATCH_STALLED_REASON_CODE,
    CopilotTurnHalt,
    TurnHaltKind,
    expire_output_contract_ladder_at_turn_end,
)
from skyvern.forge.sdk.copilot.turn_ownership import TurnClaimant
from tests.unit.conftest import make_copilot_context as _fresh_context

# ---------------------------------------------------------------------------
# A — fresh CopilotContext
# ---------------------------------------------------------------------------


def test_check_enforcement_on_fresh_agent_context_returns_none() -> None:
    ctx = _fresh_context()
    assert _check_enforcement(ctx) is None


def test_failed_test_nudge_counter_increments_on_fresh_context() -> None:
    ctx = _fresh_context()
    # _needs_failed_test_nudge requires test_after_update_done=True (i.e. the
    # agent already ran the workflow once) before it will nudge. Mimic that.
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.last_test_failure_reason = "something broke"
    # First call should emit and increment without AttributeError.
    assert _check_enforcement(ctx) is not None
    assert ctx.failed_test_nudge_count == 1


def _repair_contract(next_action: Any, *, has_current_url: bool = True) -> Any:
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
        DiagnosisInput,
        DiagnosisRepairContract,
        DiagnosisResult,
        RepairDecision,
        VerificationResult,
    )

    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(
            source_tool="update_and_run_blocks",
            browser_page_state={"has_current_url": has_current_url},
        ),
        diagnosis_result=DiagnosisResult(),
        repair_decision=RepairDecision(next_action=next_action),
        verification_result=VerificationResult(),
    )


def test_needs_inspect_before_repair_nudge_logic() -> None:
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import RepairNextAction

    ctx = _fresh_context()
    assert _needs_inspect_before_repair_nudge(ctx) is False  # no contract
    ctx.latest_diagnosis_repair_contract = _repair_contract(RepairNextAction.REPAIR)
    assert _needs_inspect_before_repair_nudge(ctx) is True  # repairable, reached page, unobserved
    ctx.latest_diagnosis_repair_contract = _repair_contract(RepairNextAction.NO_CHANGE)
    assert _needs_inspect_before_repair_nudge(ctx) is False  # not a repair
    ctx.latest_diagnosis_repair_contract = _repair_contract(RepairNextAction.REPAIR, has_current_url=False)
    assert _needs_inspect_before_repair_nudge(ctx) is False  # no reached page to inspect


def test_failed_test_routes_to_inspect_first_when_repairable_and_unobserved() -> None:
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import RepairNextAction

    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.latest_diagnosis_repair_contract = _repair_contract(RepairNextAction.REPAIR)
    assert _check_enforcement(ctx) == POST_FAILED_TEST_INSPECT_FIRST_NUDGE


def test_second_consecutive_per_tool_budget_trip_routes_to_stop_nudge() -> None:
    from skyvern.forge.sdk.copilot.failure_tracking import PER_TOOL_BUDGET_FAILURE_CATEGORY

    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
    # First budget trip earns one smaller-frontier retry nudge.
    assert _check_enforcement(ctx) == POST_PER_TOOL_BUDGET_NUDGE
    assert ctx.per_tool_budget_nudge_count == 1
    # Second consecutive budget trip -> finalize/STOP nudge, not another re-run.
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
    assert _check_enforcement(ctx) == POST_PER_TOOL_BUDGET_STOP_NUDGE
    assert ctx.per_tool_budget_nudge_count == 2


def test_failed_test_is_generic_once_reached_page_observed() -> None:
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import RepairNextAction

    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.latest_diagnosis_repair_contract = _repair_contract(RepairNextAction.REPAIR)
    # The agent already inspected the reached page since the failed run -> generic nudge.
    ctx.post_run_page_observation_after_failed_test = True
    ctx.post_run_page_observation_tool = "inspect_page_for_composition"
    ctx.post_run_page_observation_workflow_run_id = "wr_x"
    ctx.last_run_blocks_workflow_run_id = "wr_x"
    assert _check_enforcement(ctx) == POST_FAILED_TEST_NUDGE


# ---------------------------------------------------------------------------
# B1 — tool-call argument compaction
# ---------------------------------------------------------------------------


def test_prune_input_list_summarizes_old_tool_call_arguments() -> None:
    huge_yaml = "workflow:\n" + "  - block: x\n" * 2000  # ~18 KB
    old_call = {
        "type": "function_call",
        "name": "update_workflow",
        "arguments": json.dumps({"workflow_yaml": huge_yaml, "description": "initial"}),
    }
    # Four recent tool calls so the old one is outside the KEEP_RECENT window.
    recent_calls = [
        {
            "type": "function_call",
            "name": "run_blocks_and_collect_debug",
            "arguments": json.dumps({"block_labels": [f"b{i}"]}),
        }
        for i in range(4)
    ]
    items = [old_call] + recent_calls

    pruned = _prune_input_list(items)

    # Oldest call's arguments should be compacted; recent ones untouched.
    pruned_args = json.loads(pruned[0]["arguments"])
    assert "workflow_yaml" in pruned_args
    assert isinstance(pruned_args["workflow_yaml"], str)
    assert "truncated" in pruned_args["workflow_yaml"]
    for item in pruned[-3:]:
        assert "truncated" not in item["arguments"]


def test_prune_input_list_preserves_small_arguments() -> None:
    small_call = {
        "type": "function_call",
        "name": "navigate_browser",
        "arguments": json.dumps({"url": "https://example.com"}),
    }
    pruned = _prune_input_list([small_call])
    assert pruned[0]["arguments"] == small_call["arguments"]


# ---------------------------------------------------------------------------
# C — suspicious-success nudge re-fires if agent ignores it
# ---------------------------------------------------------------------------


def test_suspicious_success_nudge_refires_on_subsequent_turn() -> None:
    ctx = _fresh_context()
    ctx.last_test_ok = None
    ctx.last_test_suspicious_success = True

    first = _check_enforcement(ctx)
    assert first == POST_SUSPICIOUS_SUCCESS_NUDGE
    # Without a rerun, the flag must still be set so the nudge fires again.
    assert ctx.last_test_suspicious_success is True
    second = _check_enforcement(ctx)
    assert second == POST_SUSPICIOUS_SUCCESS_NUDGE


# ---------------------------------------------------------------------------
# L — overflow recovery strips images
# ---------------------------------------------------------------------------


def test_strip_input_images_replaces_image_parts_with_placeholder() -> None:
    payload: list[Any] = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "see this:"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA" * 1000},
            ],
        }
    ]
    stripped, did_strip = _strip_input_images(payload)
    assert did_strip is True
    assert isinstance(stripped, list)
    content = stripped[0]["content"]
    assert content[0] == {"type": "input_text", "text": "see this:"}
    assert content[1] == {"type": "input_text", "text": SCREENSHOT_PLACEHOLDER}


def test_strip_input_images_no_images_reports_false() -> None:
    payload: list[Any] = [{"role": "user", "content": [{"type": "input_text", "text": "no images here"}]}]
    stripped, did_strip = _strip_input_images(payload)
    assert did_strip is False
    assert stripped == payload


@pytest.mark.asyncio
async def test_recover_from_context_overflow_strips_images_without_session() -> None:
    current_input: list[Any] = [
        {
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA" * 1000},
            ],
        }
    ]
    recovered, stripped = await _recover_from_context_overflow(session=None, current_input=current_input)
    assert stripped is True
    assert isinstance(recovered, list)
    assert recovered[0]["content"][0]["type"] == "input_text"


class _FakeSession:
    def __init__(self) -> None:
        self.items: list[Any] = []
        self.cleared = False

    async def get_items(self) -> list[Any]:
        return list(self.items)

    async def clear_session(self) -> None:
        self.cleared = True
        self.items = []

    async def add_items(self, items: list[Any]) -> None:
        self.items.extend(items)


@pytest.mark.asyncio
async def test_recover_from_context_overflow_with_session_strips_current_input() -> None:
    # Session pruning covers history; current_input still needs its images
    # stripped — that's the case the old code missed.
    session = _FakeSession()
    session.items = [{"role": "user", "content": "old"}]
    current_input: list[Any] = [
        {
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA" * 1000},
            ],
        }
    ]
    recovered, stripped = await _recover_from_context_overflow(session=session, current_input=current_input)
    assert stripped is True
    assert isinstance(recovered, list)
    assert recovered[0]["content"][0]["type"] == "input_text"
    assert session.cleared is True


# ---------------------------------------------------------------------------
# M — navigate_enforcement_done resets on new navigate
# ---------------------------------------------------------------------------


def test_update_enforcement_from_tool_resets_navigate_latch_on_new_navigate() -> None:
    ctx = _fresh_context()
    # Simulate: first navigate + nudge already fired.
    ctx.navigate_called = True
    ctx.observation_after_navigate = False
    ctx.navigate_enforcement_done = True

    _update_enforcement_from_tool(ctx, "navigate_browser", {"ok": True, "data": {}})

    assert ctx.navigate_called is True
    assert ctx.observation_after_navigate is False
    assert ctx.navigate_enforcement_done is False


def test_check_enforcement_refires_navigate_nudge_after_latch_reset() -> None:
    ctx = _fresh_context()
    # First navigate-without-observe: nudge fires, latch set.
    ctx.navigate_called = True
    ctx.observation_after_navigate = False
    assert _check_enforcement(ctx) == POST_NAVIGATE_NUDGE
    assert ctx.navigate_enforcement_done is True

    # Agent re-navigates without observing; the streaming adapter re-arms the latch.
    _update_enforcement_from_tool(ctx, "navigate_browser", {"ok": True, "data": {}})
    # Nudge fires again on the new cycle.
    assert _check_enforcement(ctx) == POST_NAVIGATE_NUDGE


# ---------------------------------------------------------------------------
# F — _is_context_window_error is narrow enough
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg, expected",
    [
        ("context_length_exceeded: 250000 > 128000", True),
        ("This model's maximum context length is 128000 tokens", True),
        ("Please reduce the length of the messages", True),
        ("context window exceeded", True),
        ("max_tokens_per_request quota hit", False),
        ("rate_limit_exceeded", False),
        ("Some unrelated server error", False),
    ],
)
def test_is_context_window_error_matches_only_overflow_variants(msg: str, expected: bool) -> None:
    assert _is_context_window_error(Exception(msg)) is expected


def test_code_authoring_churn_backstop_raises_at_ceiling() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS

    with pytest.raises(CopilotTurnHalt) as excinfo:
        _check_enforcement(ctx)

    assert excinfo.value.halt.kind is TurnHaltKind.LOOP_DETECTED
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "code_authoring_guardrail_churn"


def test_code_authoring_churn_backstop_does_not_raise_below_ceiling() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS - 1

    assert _check_enforcement(ctx) is None


def test_code_authoring_churn_backstop_defers_to_terminal_blocker() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
    terminal = CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text="A site verification challenge blocked the run.",
        user_facing_reason="The site's verification challenge blocked the run.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
        blocked_tool="update_and_run_blocks",
    )
    ctx.blocker_signal = terminal

    with pytest.raises(CopilotTurnHalt) as excinfo:
        _check_enforcement(ctx)

    assert excinfo.value.halt.kind is TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE
    assert ctx.blocker_signal is terminal


def test_code_authoring_churn_backstop_defers_to_newly_detected_site_block() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
    ctx.probable_site_block_streak_count = PROBABLE_SITE_BLOCK_STREAK_STOP_AT
    assert ctx.blocker_signal is None

    with pytest.raises(CopilotTurnHalt) as excinfo:
        _check_enforcement(ctx)

    assert excinfo.value.halt.kind is TurnHaltKind.PROBABLE_SITE_BLOCK
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "probable_site_block_stop"


def test_code_authoring_churn_backstop_fires_when_site_block_nudge_cap_spent() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
    ctx.probable_site_block_streak_count = PROBABLE_SITE_BLOCK_STREAK_STOP_AT
    ctx.probable_site_block_stop_nudge_count = MAX_PROBABLE_SITE_BLOCK_STOP_NUDGES

    with pytest.raises(CopilotTurnHalt) as excinfo:
        _check_enforcement(ctx)

    assert excinfo.value.halt.kind is TurnHaltKind.LOOP_DETECTED
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "code_authoring_guardrail_churn"


def test_code_authoring_churn_backstop_yields_to_non_retriable_nav_error() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
    ctx.last_test_ok = False
    ctx.last_test_non_retriable_nav_error = (
        "Failed to navigate to url https://does-not-resolve.example. Error message: net::ERR_NAME_NOT_RESOLVED"
    )

    nudge = _check_enforcement(ctx)

    assert nudge is not None
    assert ctx.blocker_signal is None
    with pytest.raises(CopilotNonRetriableNavError):
        _maybe_raise_non_retriable_nav(ctx)


def _ceiling_reached_contract() -> DiagnosisRepairContract:
    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(source_tool="update_and_run_blocks"),
        diagnosis_result=DiagnosisResult(),
        repair_decision=RepairDecision(),
        verification_result=VerificationResult(),
        repair_loop_state=RepairLoopState(consecutive_identical_repair_count=3, ceiling_reached=True),
    )


def _mark_recorded_run_backed(ctx: CopilotContext) -> None:
    ctx.recorded_persisted_block_run_workflow_run_id = "wr_1"


def test_zero_run_ceiling_yields_to_code_authoring_churn_backstop() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
    ctx.latest_diagnosis_repair_contract = _ceiling_reached_contract()

    with pytest.raises(CopilotTurnHalt) as excinfo:
        _check_enforcement(ctx)

    assert excinfo.value.halt.kind is TurnHaltKind.LOOP_DETECTED
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "code_authoring_guardrail_churn"


def test_run_backed_ceiling_precedes_code_authoring_churn_backstop() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
    ctx.latest_diagnosis_repair_contract = _ceiling_reached_contract()
    ctx.last_run_blocks_workflow_run_id = "wr_1"
    _mark_recorded_run_backed(ctx)

    with pytest.raises(CopilotTurnHalt) as excinfo:
        _check_enforcement(ctx)

    assert excinfo.value.halt.kind is TurnHaltKind.REPAIR_CEILING_REACHED
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "repair_ceiling_reached"


def test_stale_fallback_run_id_does_not_make_ceiling_run_backed() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
    ctx.latest_diagnosis_repair_contract = _ceiling_reached_contract()
    ctx.last_run_blocks_workflow_run_id = "wr_stale"

    with pytest.raises(CopilotTurnHalt) as excinfo:
        _check_enforcement(ctx)

    assert excinfo.value.halt.kind is TurnHaltKind.LOOP_DETECTED
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "code_authoring_guardrail_churn"


def test_workflow_edit_clears_recorded_persisted_run_latch() -> None:
    ctx = _fresh_context()
    ctx.last_run_blocks_workflow_run_id = "wr_1"
    ctx.recorded_persisted_block_run_workflow_run_id = "wr_1"

    clear_terminal_evidence_on_workflow_edit(ctx)

    assert ctx.last_run_blocks_workflow_run_id is None
    assert ctx.recorded_persisted_block_run_workflow_run_id is None


def test_credential_priority_churn_raises_at_higher_bound() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS
    ctx.last_code_authoring_reject_was_credential_priority = True

    with pytest.raises(CopilotTurnHalt) as excinfo:
        _check_enforcement(ctx)

    assert excinfo.value.halt.kind is TurnHaltKind.LOOP_DETECTED
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "credential_priority_authoring_churn"
    assert "verify the saved-credential login" in signal.user_facing_reason


def test_credential_priority_churn_defers_below_higher_bound() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS - 1
    ctx.last_code_authoring_reject_was_credential_priority = True

    assert _check_enforcement(ctx) is None
    assert ctx.blocker_signal is None


def test_zero_run_ceiling_yields_to_credential_priority_churn() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS
    ctx.last_code_authoring_reject_was_credential_priority = True
    ctx.latest_diagnosis_repair_contract = _ceiling_reached_contract()

    with pytest.raises(CopilotTurnHalt) as excinfo:
        _check_enforcement(ctx)

    assert excinfo.value.halt.kind is TurnHaltKind.LOOP_DETECTED
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "credential_priority_authoring_churn"


def test_run_backed_ceiling_precedes_credential_priority_churn() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS
    ctx.last_code_authoring_reject_was_credential_priority = True
    ctx.latest_diagnosis_repair_contract = _ceiling_reached_contract()
    ctx.last_run_blocks_workflow_run_id = "wr_1"
    _mark_recorded_run_backed(ctx)

    with pytest.raises(CopilotTurnHalt) as excinfo:
        _check_enforcement(ctx)

    assert excinfo.value.halt.kind is TurnHaltKind.REPAIR_CEILING_REACHED
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "repair_ceiling_reached"


def test_no_progress_interaction_floor_raises_at_ceiling() -> None:
    ctx = _fresh_context()
    ctx.consecutive_no_progress_interaction_count = MAX_NO_PROGRESS_INTERACTION_ATTEMPTS

    with pytest.raises(CopilotTurnHalt) as excinfo:
        _check_enforcement(ctx)

    assert excinfo.value.halt.kind is TurnHaltKind.LOOP_DETECTED
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "loop_detected_no_forward_progress_interaction"
    assert signal.renders_final_reply is True
    assert signal.recovery_hint == "report_blocker_to_user"


def test_no_progress_interaction_floor_does_not_raise_below_ceiling() -> None:
    ctx = _fresh_context()
    ctx.consecutive_no_progress_interaction_count = MAX_NO_PROGRESS_INTERACTION_ATTEMPTS - 1

    assert _check_enforcement(ctx) is None
    assert ctx.blocker_signal is None


def test_zero_run_ceiling_yields_to_no_progress_interaction_floor() -> None:
    ctx = _fresh_context()
    ctx.consecutive_no_progress_interaction_count = MAX_NO_PROGRESS_INTERACTION_ATTEMPTS
    ctx.latest_diagnosis_repair_contract = _ceiling_reached_contract()

    with pytest.raises(CopilotTurnHalt) as excinfo:
        _check_enforcement(ctx)

    assert excinfo.value.halt.kind is TurnHaltKind.LOOP_DETECTED
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "loop_detected_no_forward_progress_interaction"


def test_run_backed_ceiling_precedes_no_progress_interaction_floor() -> None:
    ctx = _fresh_context()
    ctx.consecutive_no_progress_interaction_count = MAX_NO_PROGRESS_INTERACTION_ATTEMPTS
    ctx.latest_diagnosis_repair_contract = _ceiling_reached_contract()
    ctx.last_run_blocks_workflow_run_id = "wr_1"
    _mark_recorded_run_backed(ctx)

    with pytest.raises(CopilotTurnHalt) as excinfo:
        _check_enforcement(ctx)

    assert excinfo.value.halt.kind is TurnHaltKind.REPAIR_CEILING_REACHED
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "repair_ceiling_reached"


def test_no_progress_interaction_floor_yields_to_non_retriable_nav_error() -> None:
    ctx = _fresh_context()
    ctx.consecutive_no_progress_interaction_count = MAX_NO_PROGRESS_INTERACTION_ATTEMPTS
    ctx.last_test_ok = False
    ctx.last_test_non_retriable_nav_error = (
        "Failed to navigate to url https://does-not-resolve.example. Error message: net::ERR_NAME_NOT_RESOLVED"
    )

    nudge = _check_enforcement(ctx)

    assert nudge is not None
    assert ctx.blocker_signal is None
    with pytest.raises(CopilotNonRetriableNavError):
        _maybe_raise_non_retriable_nav(ctx)


def test_register_no_progress_interaction_click_stashes_blocker_at_cap() -> None:
    ctx = _fresh_context()
    ctx.consecutive_no_progress_interaction_count = MAX_NO_PROGRESS_INTERACTION_ATTEMPTS - 1

    register_no_progress_interaction_click(ctx, outcome="click_failed")

    assert ctx.consecutive_no_progress_interaction_count == MAX_NO_PROGRESS_INTERACTION_ATTEMPTS
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "loop_detected_no_forward_progress_interaction"


def test_register_no_progress_interaction_click_below_cap_does_not_stash() -> None:
    ctx = _fresh_context()

    register_no_progress_interaction_click(ctx, outcome="hollow")

    assert ctx.consecutive_no_progress_interaction_count == 1
    assert ctx.blocker_signal is None


def test_register_no_progress_interaction_click_defers_to_terminal_held_blocker() -> None:
    ctx = _fresh_context()
    ctx.consecutive_no_progress_interaction_count = MAX_NO_PROGRESS_INTERACTION_ATTEMPTS - 1
    terminal = CopilotToolBlockerSignal(
        blocker_kind="loop_detected",
        agent_steering_text="The repair made no progress.",
        user_facing_reason="I couldn't get past the same problem after several attempts.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="repair_ceiling_reached",
        blocked_tool="update_and_run_blocks",
    )
    ctx.blocker_signal = terminal

    register_no_progress_interaction_click(ctx, outcome="click_failed")

    assert ctx.blocker_signal is terminal


def test_reset_no_progress_interaction_count_clears_counter() -> None:
    ctx = _fresh_context()
    ctx.consecutive_no_progress_interaction_count = 3

    reset_no_progress_interaction_count(ctx)

    assert ctx.consecutive_no_progress_interaction_count == 0


def _hit_no_progress_cap(ctx: CopilotContext) -> CopilotToolBlockerSignal:
    for _ in range(MAX_NO_PROGRESS_INTERACTION_ATTEMPTS):
        register_no_progress_interaction_click(ctx, outcome="hollow")
    held = ctx.blocker_signal
    assert isinstance(held, CopilotToolBlockerSignal)
    assert held.internal_reason_code == "loop_detected_no_forward_progress_interaction"
    assert held.renders_final_reply is True
    return held


def test_no_progress_reset_clears_held_blocker_after_cap() -> None:
    ctx = _fresh_context()
    _hit_no_progress_cap(ctx)

    reset_no_progress_interaction_count(ctx)

    assert ctx.consecutive_no_progress_interaction_count == 0
    assert ctx.blocker_signal is None
    assert ctx.latest_tool_blocker_signal is None


def test_no_progress_reset_at_progress_seam_stops_re_halt() -> None:
    ctx = _fresh_context()
    _hit_no_progress_cap(ctx)

    reset_no_progress_interaction_count(ctx)

    assert ctx.consecutive_no_progress_interaction_count == 0
    assert ctx.blocker_signal is None
    assert _check_enforcement(ctx) is None


@pytest.mark.parametrize("recovery_tool", ["evaluate", "inspect_page_for_composition"])
def test_no_progress_held_blocker_survives_progress_tool_success(recovery_tool: str) -> None:
    ctx = _fresh_context()
    held = _hit_no_progress_cap(ctx)

    maybe_clear_blocker_signal_on_tool_success(ctx, recovery_tool)

    assert ctx.blocker_signal is held


def _grant_output_contract_ladder(ctx: CopilotContext) -> None:
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.GRANTED


def test_inline_churn_reject_yields_to_live_ladder_and_keeps_count() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS - 1
    _grant_output_contract_ladder(ctx)

    _record_code_authoring_guardrail_reject(ctx)

    assert ctx.code_authoring_guardrail_reject_count == MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert any(
        event.fingerprint == "output_contract_actuation>code_authoring_guardrail_churn"
        for event in ctx.gate_precedence_conflict_events
    )


def test_inline_churn_reject_stashes_when_no_owner_is_live() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS - 1

    _record_code_authoring_guardrail_reject(ctx)

    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == "code_authoring_guardrail_churn"
    assert signal.renders_final_reply is True


def test_churn_backstop_yields_to_live_ladder_without_halt_or_stash() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS
    _grant_output_contract_ladder(ctx)

    assert _check_enforcement(ctx) is None

    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert ctx.output_contract_actuation_by_signature["sig_a"] == OutputContractAdvisoryState.GRANTED
    assert any(
        event.fingerprint == "output_contract_actuation>code_authoring_guardrail_churn"
        for event in ctx.gate_precedence_conflict_events
    )


def test_no_progress_backstop_yields_to_live_ladder() -> None:
    ctx = _fresh_context()
    ctx.consecutive_no_progress_interaction_count = MAX_NO_PROGRESS_INTERACTION_ATTEMPTS
    _grant_output_contract_ladder(ctx)

    assert _check_enforcement(ctx) is None

    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert any(
        event.fingerprint == "output_contract_actuation>loop_detected" for event in ctx.gate_precedence_conflict_events
    )


def test_register_no_progress_click_yields_to_live_ladder() -> None:
    ctx = _fresh_context()
    ctx.consecutive_no_progress_interaction_count = MAX_NO_PROGRESS_INTERACTION_ATTEMPTS - 1
    _grant_output_contract_ladder(ctx)

    register_no_progress_interaction_click(ctx, outcome="failed")

    assert ctx.consecutive_no_progress_interaction_count == MAX_NO_PROGRESS_INTERACTION_ATTEMPTS
    assert ctx.blocker_signal is None


def test_grant_plus_ceiling_reject_in_one_call_reaches_stalled_terminal_not_churn_reply() -> None:
    ctx = _fresh_context()
    ctx.code_authoring_guardrail_reject_count = MAX_CODE_AUTHORING_GUARDRAIL_REJECTS - 1
    _grant_output_contract_ladder(ctx)
    ctx.output_contract_pending_run_evidence["sig_a"] = ["output.confirmation_number"]

    _record_code_authoring_guardrail_reject(ctx)
    assert _check_enforcement(ctx) is None
    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None

    halt = expire_output_contract_ladder_at_turn_end(ctx)

    assert halt is not None
    assert ctx.turn_halt is halt
    assert ctx.output_contract_actuation_by_signature["sig_a"] == OutputContractAdvisoryState.EXPIRED
    signal = ctx.blocker_signal
    assert isinstance(signal, CopilotToolBlockerSignal)
    assert signal.internal_reason_code == ADVISORY_DISPATCH_STALLED_REASON_CODE


def test_single_captured_interaction_trajectory_never_reaches_goal() -> None:
    ctx = _fresh_context()
    ctx.scout_trajectory = [
        {
            "tool_name": "type_text",
            "selector": "#confirmation",
            "source_url": "https://portal.example.com/order-status",
            "role": "textbox",
            "accessible_name": "Confirmation number",
            "typed_length": 8,
            "trajectory_index": 0,
        }
    ]

    assert synthesized_trajectory_reaches_goal(ctx) is False


def test_scouted_spine_missing_text_renders_non_uncovered_families() -> None:
    dropped = ObligationFinding(
        kind=UNFORGIVEN_DROP_FINDING,
        record={"tool_name": "fill_credential_field", "reason_code": "strict_selector", "trajectory_index": 2},
    )
    text = _scouted_spine_missing_text([dropped])
    assert text
    assert "fill_credential_field" in text


def test_missing_steps_listed_on_give_up_offer_and_anchored_in_held_signal() -> None:
    ctx = _fresh_context()
    ctx.has_staged_proposal = True
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="loop_detected",
        agent_steering_text="The repair made no progress.",
        user_facing_reason="I kept the draft.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="repair_ceiling_reached",
        blocked_tool="update_workflow",
    )
    reply = _with_scouted_spine_missing_steps(ctx, "I kept the draft.", "`fill` on '#totp'")
    assert "`fill` on '#totp'" in reply
    assert "`fill` on '#totp'" in ctx.blocker_signal.user_facing_reason


def test_missing_steps_not_appended_without_staged_proposal() -> None:
    ctx = _fresh_context()
    ctx.has_staged_proposal = False
    assert _with_scouted_spine_missing_steps(ctx, "base", "`fill` on '#totp'") == "base"


def _unrelated_owner_give_up_ctx(internal_reason_code: str) -> tuple[CopilotContext, str]:
    ctx = _fresh_context()
    ctx.has_staged_proposal = True
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.impose_synthesized_code_block = True
    ctx.persisted_draft_browser_calls = []
    ctx.scout_trajectory = [
        {
            "tool_name": "click",
            "selector": "#search-submit",
            "source_url": "https://example.com/search",
            "trajectory_index": 0,
        }
    ]
    unrelated_reason = "I couldn't finish this after several attempts. Tell me what to change and I'll try again."
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text="An unrelated blocker owns the turn.",
        user_facing_reason=unrelated_reason,
        recovery_hint="report_blocker_to_user",
        internal_reason_code=internal_reason_code,
        blocked_tool="update_workflow",
    )
    ctx.blocker_signal_claimant = TurnClaimant.OUTPUT_CONTRACT_ACTUATION
    return ctx, unrelated_reason


def test_finalizer_names_missing_steps_when_unrelated_blocker_renders_give_up() -> None:
    ctx, unrelated_reason = _unrelated_owner_give_up_ctx("output_contract_actuation_exhausted")
    result = AgentResult(user_response="agent prose", updated_workflow=None, global_llm_context=None)

    overridden = _finalize_result_with_blocker_override(ctx, result, exit_site="test")

    assert "#search-submit" in overridden.user_response
    assert ctx.blocker_signal.user_facing_reason == unrelated_reason
    assert ctx.blocker_signal_claimant is TurnClaimant.OUTPUT_CONTRACT_ACTUATION


@pytest.mark.parametrize(
    "internal_reason_code",
    ["repair_ceiling_reached", "completion_contract_unsatisfied", "output_contract_actuation_exhausted"],
)
def test_finalizer_render_exit_names_missing_steps_per_owner(internal_reason_code: str) -> None:
    ctx, unrelated_reason = _unrelated_owner_give_up_ctx(internal_reason_code)
    result = AgentResult(user_response="agent prose", updated_workflow=None, global_llm_context=None)

    overridden = _finalize_result_with_blocker_override(ctx, result, exit_site="test")

    assert "#search-submit" in overridden.user_response
    assert overridden.user_response.count("This draft is still missing steps you demonstrated:") == 1
    assert ctx.blocker_signal.user_facing_reason == unrelated_reason
    assert ctx.blocker_signal_claimant is TurnClaimant.OUTPUT_CONTRACT_ACTUATION


def test_same_omission_spine_violation_still_refused_after_change() -> None:
    ctx = _fresh_context()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.impose_synthesized_code_block = True
    ctx.scout_trajectory = [
        {
            "tool_name": "click",
            "selector": "#search-submit",
            "source_url": "https://example.com/search",
            "trajectory_index": 0,
        }
    ]
    omitting_draft = (
        "title: t\n"
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: report_only\n"
        "    code: |\n"
        '      print(await page.locator("body").inner_text())\n'
    )
    result = _pre_persist_scouted_spine_result(omitting_draft, ctx)
    assert result is not None
    assert result.repair_context is not None
    assert result.repair_context.reason_code == "scouted_spine_under_build"
    assert "#search-submit" in result.violations[0]

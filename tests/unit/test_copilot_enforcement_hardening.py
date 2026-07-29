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

from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
    clear_terminal_evidence_on_workflow_edit,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import UNFORGIVEN_DROP_FINDING, ObligationFinding
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisInput,
    DiagnosisRepairContract,
    DiagnosisResult,
    RepairDecision,
    RepairLoopState,
    VerificationResult,
)
from skyvern.forge.sdk.copilot.enforcement import (
    POST_FAILED_TEST_INSPECT_FIRST_NUDGE,
    POST_FAILED_TEST_NUDGE,
    POST_NAVIGATE_NUDGE,
    POST_PER_TOOL_BUDGET_NUDGE,
    POST_PER_TOOL_BUDGET_STOP_NUDGE,
    POST_SUSPICIOUS_SUCCESS_NUDGE,
    SCREENSHOT_PLACEHOLDER,
    _check_enforcement,
    _code_authoring_reject_count_resets,
    _is_context_window_error,
    _needs_inspect_before_repair_nudge,
    _prune_input_list,
    _recover_from_context_overflow,
    _scouted_spine_missing_text,
    _strip_input_images,
    register_no_progress_interaction_click,
    reset_no_progress_interaction_count,
    synthesized_trajectory_reaches_goal,
)
from skyvern.forge.sdk.copilot.output_contracts import OutputContractAdvisoryState
from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool
from skyvern.forge.sdk.copilot.tools.workflow_update import _pre_persist_scouted_spine_result
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


def _repair_streak_contract() -> DiagnosisRepairContract:
    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(source_tool="update_and_run_blocks"),
        diagnosis_result=DiagnosisResult(),
        repair_decision=RepairDecision(),
        verification_result=VerificationResult(),
        repair_loop_state=RepairLoopState(consecutive_identical_repair_count=3),
    )


def _mark_recorded_run_backed(ctx: CopilotContext) -> None:
    ctx.recorded_persisted_block_run_workflow_run_id = "wr_1"


def test_workflow_edit_clears_recorded_persisted_run_latch() -> None:
    ctx = _fresh_context()
    ctx.last_run_blocks_workflow_run_id = "wr_1"
    ctx.recorded_persisted_block_run_workflow_run_id = "wr_1"

    clear_terminal_evidence_on_workflow_edit(ctx)

    assert ctx.last_run_blocks_workflow_run_id is None
    assert ctx.recorded_persisted_block_run_workflow_run_id is None


def test_register_no_progress_interaction_click_below_cap_does_not_stash() -> None:
    ctx = _fresh_context()

    register_no_progress_interaction_click(ctx, outcome="hollow")

    assert ctx.consecutive_no_progress_interaction_count == 1
    assert ctx.blocker_signal is None


def test_reset_no_progress_interaction_count_clears_counter() -> None:
    ctx = _fresh_context()
    ctx.consecutive_no_progress_interaction_count = 3

    reset_no_progress_interaction_count(ctx)

    assert ctx.consecutive_no_progress_interaction_count == 0


def _grant_output_contract_ladder(ctx: CopilotContext) -> None:
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.GRANTED


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


def test_reject_count_resets_only_on_non_repeat_without_frontier_unchanged() -> None:
    assert _code_authoring_reject_count_resets(False, False) is True
    assert _code_authoring_reject_count_resets(False, True) is False
    assert _code_authoring_reject_count_resets(None, False) is False
    assert _code_authoring_reject_count_resets(True, False) is False

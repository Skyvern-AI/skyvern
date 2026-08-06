"""Tests for enforcement hardening landed in copilot-stack/06b:

* fresh ``CopilotContext`` flows through ``enforcement_decision`` without raising
  AttributeError (enforcement fields have dataclass defaults).
* ``_prune_input_list`` compacts the ``arguments`` field of older tool calls
  so large payloads (like a full workflow YAML) don't accumulate.
* ``enforcement_decision`` does NOT clear ``last_test_suspicious_success`` after
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
from types import SimpleNamespace
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
    SCREENSHOT_PLACEHOLDER,
    _code_authoring_reject_count_resets,
    _is_context_window_error,
    _needs_inspect_before_repair_nudge,
    _prune_input_list,
    _recover_from_context_overflow,
    _scouted_spine_missing_text,
    _strip_input_images,
    _witnessed_values_by_path,
    enforcement_decision,
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


def test_enforcement_decision_on_fresh_agent_context_returns_none() -> None:
    ctx = _fresh_context()
    assert enforcement_decision(ctx) is None


def test_failed_test_nudge_counter_increments_on_fresh_context() -> None:
    ctx = _fresh_context()
    # _needs_failed_test_nudge requires test_after_update_done=True (i.e. the
    # agent already ran the workflow once) before it will nudge. Mimic that.
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.last_test_failure_reason = "something broke"
    # First call should emit and increment without AttributeError.
    assert enforcement_decision(ctx) is not None
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
    assert enforcement_decision(ctx).rule == "post_failed_test_inspect_first"


def test_second_consecutive_per_tool_budget_trip_routes_to_stop_nudge() -> None:
    from skyvern.forge.sdk.copilot.failure_tracking import PER_TOOL_BUDGET_FAILURE_CATEGORY

    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
    # First budget trip earns one smaller-frontier retry nudge.
    assert enforcement_decision(ctx).rule == "post_per_tool_budget"
    assert ctx.per_tool_budget_nudge_count == 1
    # Second consecutive budget trip -> finalize/STOP nudge, not another re-run.
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
    assert enforcement_decision(ctx).rule == "post_per_tool_budget_stop"
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
    assert enforcement_decision(ctx).rule == "post_failed_test"


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

    first = enforcement_decision(ctx)
    assert first.rule == "post_suspicious_success"
    # Without a rerun, the flag must still be set so the nudge fires again.
    assert ctx.last_test_suspicious_success is True
    second = enforcement_decision(ctx)
    assert second.rule == "post_suspicious_success"


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


def test_enforcement_decision_refires_navigate_nudge_after_latch_reset() -> None:
    ctx = _fresh_context()
    # First navigate-without-observe: nudge fires, latch set.
    ctx.navigate_called = True
    ctx.observation_after_navigate = False
    assert enforcement_decision(ctx).rule == "post_navigate"
    assert ctx.navigate_enforcement_done is True

    # Agent re-navigates without observing; the streaming adapter re-arms the latch.
    _update_enforcement_from_tool(ctx, "navigate_browser", {"ok": True, "data": {}})
    # Nudge fires again on the new cycle.
    assert enforcement_decision(ctx).rule == "post_navigate"


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


# ---------------------------------------------------------------------------
# Repair obligation — a turn may not finalize a draft its own build test disproved
# ---------------------------------------------------------------------------


class _FakeFinalResult:
    """Minimal stand-in for RunResultStreaming carrying one final model response."""

    def __init__(self, response_type: str, user_response: str) -> None:
        self._payload = json.dumps({"type": response_type, "user_response": user_response})

    @property
    def final_output(self) -> str:
        return self._payload


def _failed_run_ctx(next_action: Any, *, observed: bool = True) -> Any:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.latest_diagnosis_repair_contract = _repair_contract(next_action)
    if observed:
        ctx.post_run_page_observation_after_failed_test = True
        ctx.post_run_page_observation_tool = "inspect_page_for_composition"
        ctx.post_run_page_observation_workflow_run_id = "wr_x"
        ctx.last_run_blocks_workflow_run_id = "wr_x"
    return ctx


def test_observed_reply_cannot_finalize_while_repair_is_owed() -> None:
    """The production shape: failed run -> inspect once -> report the blocker -> turn ends."""
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import RepairNextAction

    ctx = _failed_run_ctx(RepairNextAction.REPAIR)
    result = _FakeFinalResult("REPLY", "The latest bill link opens an email-delivery form.")

    assert enforcement_decision(ctx, result) is not None


def test_observed_reply_finalizes_once_repair_is_discharged() -> None:
    """A contract that no longer asks for repair releases the turn — the obligation is typed."""
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import RepairNextAction

    ctx = _failed_run_ctx(RepairNextAction.NO_CHANGE)
    result = _FakeFinalResult("REPLY", "Downloaded the latest invoice.")

    assert enforcement_decision(ctx, result) is None


def test_ask_question_still_finalizes_while_repair_is_owed() -> None:
    """Needing the user is a legitimate exit; another repair round cannot supply the answer."""
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import RepairNextAction

    ctx = _failed_run_ctx(RepairNextAction.REPAIR)
    ctx.failed_test_nudge_count = 99  # counters exhausted
    result = _FakeFinalResult("ASK_QUESTION", "Which account should I use?")

    assert enforcement_decision(ctx, result) is None


def test_exhausted_nudge_counters_do_not_release_an_open_repair_obligation() -> None:
    """Counters bound nudge repetition; they were never evidence the failure was addressed."""
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import RepairNextAction

    ctx = _failed_run_ctx(RepairNextAction.REPAIR)
    ctx.failed_test_nudge_count = 99
    result = _FakeFinalResult("REPLY", "I drafted the workflow; it attempts to download the bill.")

    assert enforcement_decision(ctx, result).rule == "post_failed_test"


def test_stop_decision_releases_the_turn_even_with_counters_exhausted() -> None:
    """Typed terminal evidence, not a counter, is what ends a repairable failure."""
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import RepairNextAction

    ctx = _failed_run_ctx(RepairNextAction.STOP)
    ctx.failed_test_nudge_count = 99
    result = _FakeFinalResult("REPLY", "This site requires a mailed statement request.")

    assert enforcement_decision(ctx, result) is None


def test_repair_obligation_releases_the_turn_once_its_rounds_are_spent() -> None:
    """A failure that looks repairable but is not must still be reportable, not re-nudged forever."""
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import RepairNextAction
    from skyvern.forge.sdk.copilot.enforcement import MAX_REPAIR_OBLIGATION_NUDGES

    ctx = _failed_run_ctx(RepairNextAction.REPAIR)
    ctx.failed_test_nudge_count = 99
    result = _FakeFinalResult("REPLY", "This site offers no downloadable statement.")

    # Held open while rounds remain.
    assert enforcement_decision(ctx, result).rule == "post_failed_test"

    ctx.repair_obligation_nudge_count = MAX_REPAIR_OBLIGATION_NUDGES
    assert enforcement_decision(ctx, result) is None


class TestWitnessArbitration:
    def _ctx(self, reads: list[str]) -> SimpleNamespace:
        packet = {
            "source_tool": "inspect_page_for_composition",
            "key_value_relations": [
                {"key_text": "logs found", "value_text": "1.41K", "visible": True, "value_visible": True}
            ],
        }
        return SimpleNamespace(
            flow_evidence=[{"evidence": packet, "reached_via": "current_page", "had_bounded_schema": True, "step": 0}],
            scout_trajectory=[
                {
                    "tool_name": "read_value",
                    "read_output_path": "output.azure_error_count",
                    "read_output_path_source": "declared",
                    "read_result_value": value,
                }
                for value in reads
            ],
        )

    def test_conflicting_reads_resolve_to_the_value_the_page_still_shows(self) -> None:
        # Live shape (SKY-13226): a login-form probe and the real read both claimed the path; dropping
        # the pair left the one requested output with no witness at all.
        ctx = self._ctx(['{"passwordId":"password"}', "1.41K"])
        assert _witnessed_values_by_path(ctx) == {"output.azure_error_count": "1.41K"}

    def test_a_conflict_the_page_corroborates_for_none_still_carries_no_witness(self) -> None:
        ctx = self._ctx(["junk-a", "junk-b"])
        assert _witnessed_values_by_path(ctx) == {}

    def test_a_read_that_only_inherited_the_path_witnesses_nothing(self) -> None:
        # An early probe of a login form was promoted to the requested output because it was the only
        # path on offer, and its JSON then stood as the witness for a figure the page had not shown.
        ctx = self._ctx([])
        ctx.scout_trajectory = [
            {
                "tool_name": "read_value",
                "read_output_path": "output.azure_error_count",
                "read_output_path_source": "elimination",
                "read_result_value": '{"passwordId": "password"}',
            }
        ]

        assert _witnessed_values_by_path(ctx) == {}

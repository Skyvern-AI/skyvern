from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _source(relative_path: str) -> str:
    return (_REPO_ROOT / relative_path).read_text()


def test_deleted_author_time_refusal_plane_has_no_production_residue() -> None:
    production_sources = {
        path: _source(path)
        for path in (
            "skyvern/forge/sdk/copilot/blocker_signal.py",
            "skyvern/forge/sdk/copilot/agent.py",
            "skyvern/forge/sdk/copilot/build_test_outcome.py",
            "skyvern/forge/sdk/copilot/config.py",
            "skyvern/forge/sdk/copilot/context.py",
            "skyvern/forge/sdk/copilot/diagnosis_repair_contract.py",
            "skyvern/forge/sdk/copilot/enforcement.py",
            "skyvern/forge/sdk/copilot/mcp_adapter.py",
            "skyvern/forge/sdk/copilot/output_utils.py",
            "skyvern/forge/sdk/copilot/runtime.py",
            "skyvern/forge/sdk/copilot/streaming_adapter.py",
            "skyvern/forge/sdk/copilot/tools/__init__.py",
            "skyvern/forge/sdk/copilot/tools/blockers.py",
            "skyvern/forge/sdk/copilot/tools/credential_fill.py",
            "skyvern/forge/sdk/copilot/tools/composition_capture.py",
            "skyvern/forge/sdk/copilot/tools/mcp_hooks.py",
            "skyvern/forge/sdk/copilot/tools/run_execution.py",
            "skyvern/forge/sdk/copilot/tools/scouting.py",
            "skyvern/forge/sdk/copilot/tools/workflow_update.py",
            "skyvern/forge/sdk/copilot/turn_halt.py",
            "skyvern/config.py",
        )
    }
    deleted_symbols = (
        "_tool_loop_error",
        "current_page_challenge_advisory_signal",
        "detect_failed_tool_step_loop_for_ctx",
        "per_tool_budget_nudge_count",
        "MAX_PER_TOOL_BUDGET_NUDGES",
        "post_budget_page_inspection_required",
        "post_budget_page_inspection_url",
        "post_budget_page_inspection_run_id",
        "challenge_gated_proxy_retry_count",
        "_strip_intent_for_code_only_selector_action",
        "_code_only_selector_action_requires_deterministic_target",
        "retire_outranked_turn_halt",
        "build_loop_blocker_signal",
        "refresh_held_loop_blocker_evidence",
        "loop_blocker_evidence_from_ctx",
        "consecutive_tool_tracker",
        "phase_gated",
        "missing_required_context",
        "repeated_failure_streak_count",
        "repeated_failure_nudge_emitted_at_streak",
        "pending_reconciliation_run_id",
        "per_tool_budget_problem_block_labels",
        "MetadataRejectLadderInput",
        "MetadataRejectLadderState",
        "MetadataRejectLadderDecision",
        "adjudicate_metadata_reject_ladder",
        "_MAX_OUTPUT_CONTRACT_DEFERRALS",
        "_MAX_OUTPUT_CONTRACT_ACTUATIONS_WITHOUT_RUN",
        "_METADATA_FAMILY_REJECT_FAMILIES",
        "metadata_reject_ladder_state",
        "consecutive_non_converging_repair_count",
        "RecordedOutcomeGroundingPayload",
        "RecordedOutcomeGroundingRequirement",
        "RecordedOutcomeBindingConstraint",
        "RepairLoopState",
        "recorded_outcome_grounding_requirement",
        "recorded_outcome_binding_constraint",
        "consecutive_no_progress_interaction_count",
        "register_no_progress_interaction_click",
        "reset_no_progress_interaction_count",
        "repair_obligation_nudge_count",
        "post_update_nudge_count",
        "format_nudge_count",
        "no_workflow_nudge_count",
        "failed_test_nudge_count",
        "explore_without_workflow_nudge_count",
        "suspicious_success_nudge_count",
        "MAX_POST_UPDATE_NUDGES",
        "MAX_FAILED_TEST_NUDGES",
        "MAX_REPAIR_OBLIGATION_NUDGES",
        "MAX_FORMAT_NUDGES",
        "MAX_NO_WORKFLOW_NUDGES",
        "MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES",
        "MAX_SUSPICIOUS_SUCCESS_NUDGES",
        "post_explore_without_workflow",
        "post_suspicious_success",
        "post_failed_test_inspect_first",
        "post_no_workflow_delivery",
        "post_anti_bot_failed_test",
        "post_format",
        "CODE-ONLY CODE VALIDATION BLOCKED",
        "Do not use evaluate to click elements",
        "_JQUERY_SELECTOR_RE",
        "discovery_calls_made",
        "discovery_calls_this_turn",
        "last_evaluate_actionable_signature",
        "last_evaluate_actionable_url",
        "last_auto_acted_signature",
        "_auto_act_on_repeat",
        'next_action"] = "click"',
        "_attach_reperception_targets_on_non_advancing_click",
        "COPILOT_CLICK_SETTLE_MAX_PROBES",
        "COPILOT_CLICK_SETTLE_DELAY_SECONDS",
        "COPILOT_CLICK_SETTLE_DEADLINE_SECONDS",
        "COPILOT_CLICK_REPERCEPTION_ATTACH_ENABLED",
    )

    residue = {
        path: symbol for path, source in production_sources.items() for symbol in deleted_symbols if symbol in source
    }

    assert residue == {}


def test_generated_code_quality_diagnostic_is_not_a_code_safety_refusal() -> None:
    from skyvern.forge.sdk.copilot.tools.workflow_update import _code_block_safety_errors

    workflow_yaml = """\
title: Demo
workflow_definition:
  parameters: []
  blocks:
  - block_type: code
    label: extract
    code: |
      await page.locator("table").wait_for()
"""

    errors = _code_block_safety_errors(workflow_yaml, None)

    assert errors == []

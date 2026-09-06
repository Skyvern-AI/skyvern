"""Tests for enforcement pruning and null-data handling.

These cover three regressions observed in trace 019d7b5c884dff0ff648680b9f31f715:
  1. Extraction returning all-null fields was treated as success.
  2. Context grew linearly because old tool outputs kept full content.
  3. No escalation when the agent looped on the same null-data failure.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from skyvern.config import Settings, settings
from skyvern.forge.sdk.copilot import enforcement as enforcement_module
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.config import (
    SCREENSHOT_DROPPED_NUDGE,
    CopilotConfig,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    _RECENT_TOOL_OUTPUT_CHAR_CAP,
    KEEP_RECENT_TOOL_OUTPUTS,
    NUDGE_SENTINEL,
    TOTAL_TIMEOUT_SECONDS,
    _mark_copilot_total_timeout,
    _mark_copilot_total_timeout_if_elapsed,
    _prune_input_list,
    _recover_from_context_overflow,
    _summarize_tool_output,
    aggressive_prune,
    enforcement_decision,
    is_screenshot_message,
)
from skyvern.forge.sdk.copilot.output_utils import MCP_RESULT_PROVENANCE_KEY, MCP_RESULT_PROVENANCE_VALUE
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion
from skyvern.forge.sdk.copilot.tools import (
    _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
    _analyze_run_blocks,
    _is_meaningful_extracted_data,
    _record_run_blocks_result,
)
from skyvern.forge.sdk.copilot.tools._shared import TOTAL_TIMEOUT_SECONDS as shared_total_timeout_seconds
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from tests.unit.copilot_test_helpers import make_copilot_ctx


class _Ctx:
    """Minimal stand-in for CopilotContext used in enforcement checks.

    Keep this in sync with ``AgentContext`` enforcement-state fields — missing
    attributes would show up as AttributeError in the branches that use bare
    access rather than ``getattr``.
    """

    def __init__(self) -> None:
        self.navigate_called = False
        self.observation_after_navigate = False
        self.navigate_enforcement_done = False
        self.update_workflow_called = False
        self.persisted_draft_browser_calls = None
        self.test_after_update_done = False
        self.pre_run_gated_output_warning_fingerprint: tuple[tuple[str, str, bool, str], ...] = ()
        self.post_update_nudge_count = 0
        self.format_nudge_count = 0
        self.user_message = ""
        self.last_update_block_count = None
        self.last_test_ok = None
        self.last_test_failure_reason = None
        self.last_test_suspicious_success = False
        self.last_test_anti_bot = None
        self.last_failure_category_top = None
        self.failed_test_nudge_count = 0
        self.explore_without_workflow_nudge_count = 0
        self.repeated_failure_streak_count = 0
        self.repeated_failure_nudge_emitted_at_streak = 0
        self.verified_terminal_proposal_ready = False
        self.completion_verification_result = None
        self.last_artifact_health_blocker_reason = None
        self.latest_diagnosis_repair_contract = None
        self.last_code_authoring_repair_context = None
        self.synthesized_block_reopened_after_failed_run = False
        self.synthesized_goal_complete_landed = False
        self.impose_synthesized_code_block = False
        self.scouted_output_covered_paths: set[str] = set()
        self.scout_observed_terminal_criterion_ids: set[str] = set()
        self.scout_observation_contract: object | None = None
        self.flow_evidence: list[dict[str, object]] = []
        self.last_bound_requested_output_extraction_plan = None
        self.requested_output_designations: list[dict[str, object]] = []
        self.composition_page_evidence = None
        self.copilot_config: CopilotConfig | None = None
        self.latest_recorded_build_test_outcome = None
        self.last_run_blocks_workflow_run_id = None
        self.post_run_page_observation_tool = None
        self.post_run_page_observation_url = None
        self.post_run_page_observation_workflow_run_id = None
        self.post_run_page_observation_after_failed_test = False
        self.post_run_page_observation_generation = 0
        self.post_run_page_path_interaction_window = None
        self.workflow_yaml = ""
        self.workflow_verification_evidence = WorkflowVerificationEvidence()
        self.completion_criteria_turn_state = None
        self.reached_download_target: ReachedDownloadTarget | None = None
        self.request_policy = None
        self.blocker_signal = None
        self.turn_halt = None


# ---------------------------------------------------------------------------
# _is_meaningful_extracted_data
# ---------------------------------------------------------------------------


def test_meaningful_data_none() -> None:
    assert _is_meaningful_extracted_data(None) is False


def test_meaningful_data_empty_dict() -> None:
    assert _is_meaningful_extracted_data({}) is False


def test_meaningful_data_all_null_dict() -> None:
    # The regression: {"price": None} used to count as meaningful because
    # the dict itself is truthy. It must NOT count as meaningful.
    assert _is_meaningful_extracted_data({"price": None}) is False


def test_meaningful_data_nested_all_null() -> None:
    assert _is_meaningful_extracted_data({"a": None, "b": {"c": None}}) is False


def test_meaningful_data_one_real_value() -> None:
    assert _is_meaningful_extracted_data({"price": "260.48", "other": None}) is True


def test_meaningful_data_empty_list() -> None:
    assert _is_meaningful_extracted_data([]) is False


def test_meaningful_data_list_of_nulls() -> None:
    assert _is_meaningful_extracted_data([None, None]) is False


def test_meaningful_data_scalar_zero() -> None:
    # A literal 0 is still meaningful output — it's a value, not absence of data.
    assert _is_meaningful_extracted_data(0) is True


def test_meaningful_data_empty_string() -> None:
    assert _is_meaningful_extracted_data("") is False


def test_meaningful_data_string() -> None:
    assert _is_meaningful_extracted_data("$260.48") is True


def test_unrecoverable_browser_session_error_stops_after_second_failure() -> None:
    from skyvern.forge.sdk.copilot.enforcement import (
        CopilotUnrecoverableToolError,
        _maybe_raise_unrecoverable_tool_error,
    )

    ctx = SimpleNamespace(last_artifact_health_blocker_reason=None, completion_verification_result=None)
    output = {"ok": False, "error": "Browser session not found while taking screenshot (404)."}

    _maybe_raise_unrecoverable_tool_error(ctx, "get_browser_screenshot", output)
    assert ctx.unrecoverable_tool_error_streak_count == 1

    with pytest.raises(CopilotUnrecoverableToolError) as exc_info:
        _maybe_raise_unrecoverable_tool_error(ctx, "get_browser_screenshot", output)

    assert "Browser session not found" in str(exc_info.value)
    assert ctx.unrecoverable_tool_error_streak_count == 2
    contract = ctx.latest_diagnosis_repair_contract
    assert contract.repair_decision.next_action == "stop"
    assert contract.verification_result.remaining_blocker == "Browser session not found while taking screenshot (404)."


def test_unrecoverable_tool_error_ignores_regular_website_404() -> None:
    from skyvern.forge.sdk.copilot.enforcement import _maybe_raise_unrecoverable_tool_error

    ctx = SimpleNamespace()

    _maybe_raise_unrecoverable_tool_error(
        ctx,
        "navigate_browser",
        {"ok": False, "error": "The page returned HTTP 404 page not found."},
    )

    assert getattr(ctx, "unrecoverable_tool_error_streak_count", 0) == 0
    assert getattr(ctx, "latest_diagnosis_repair_contract", None) is None


def test_unrecoverable_contract_stop_preempts_failed_test_nudge() -> None:
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import build_diagnosis_repair_contract
    from skyvern.forge.sdk.copilot.enforcement import CopilotUnrecoverableToolError

    ctx = _Ctx()
    ctx.last_test_ok = False
    reason = "Browser session not found while running blocks (404)."
    ctx.latest_diagnosis_repair_contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "error": reason,
            "data": {
                "overall_status": "aborted",
                "failure_reason": reason,
                "failure_categories": [{"category": "UNRECOVERABLE_TOOL_ERROR"}],
            },
        },
        ctx=ctx,
    )

    with pytest.raises(CopilotUnrecoverableToolError):
        enforcement_decision(ctx)

    assert ctx.failed_test_nudge_count == 0


# ---------------------------------------------------------------------------
# _analyze_run_blocks — envelope-unwrap for EXTRACTION blocks
#
# ExtractionBlock stores TaskOutput.from_task() on block.output. Envelope
# fields (task_id, status, *_screenshot_artifact_ids) are always populated on
# a completed run and would short-circuit _is_meaningful_extracted_data to
# True even when the real payload fields (extracted_information,
# downloaded_files, downloaded_file_urls) are empty. The meaningful-data
# check must judge against the payload slice, not the envelope.
# ---------------------------------------------------------------------------


_EMPTY_EXTRACTION_ENVELOPE: dict[str, Any] = {
    "task_id": "tsk_00000000000000000001",
    "status": "completed",
    "extracted_information": [],
    "failure_reason": None,
    "errors": [],
    "failure_category": None,
    "downloaded_files": [],
    "downloaded_file_urls": None,
    "task_screenshots": None,
    "workflow_screenshots": None,
    "task_screenshot_artifact_ids": ["a_00000000000000000001", "a_00000000000000000002"],
    "workflow_screenshot_artifact_ids": ["a_00000000000000000001", "a_00000000000000000003"],
}


def _run_result(blocks: list[dict[str, Any]], ok: bool = True) -> dict[str, Any]:
    return {"ok": ok, "data": {"blocks": blocks}}


def _envelope(**overrides: Any) -> dict[str, Any]:
    """Return a fresh copy of the empty-extraction envelope with field overrides."""
    return {**_EMPTY_EXTRACTION_ENVELOPE, **overrides}


def _extraction_block(extracted_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": "extract_flights",
        "block_type": "EXTRACTION",
        "status": "completed",
        "extracted_data": extracted_data,
    }


def _text_prompt_block(extracted_data: Any) -> dict[str, Any]:
    return {
        "label": "summarize",
        "block_type": "TEXT_PROMPT",
        "status": "completed",
        "extracted_data": extracted_data,
    }


# Case id -> (envelope overrides, expected empty_data_blocks)
#
# empty_payload_trace_repro: extracted_information=[], downloaded_files=[],
#   downloaded_file_urls=None, envelope metadata populated. Envelope-as-a-whole
#   is truthy; real payload is empty; gate must flip. (SKY-9143 repro.)
# download_only_files / download_only_urls: legitimate extraction success where the
#   block produced files but no structured payload — must NOT flip the gate.
_EXTRACTION_ENVELOPE_CASES: list[tuple[str, dict[str, Any], bool]] = [
    ("empty_payload_trace_repro", {}, True),
    ("real_extraction", {"extracted_information": [{"price": "260.48"}]}, False),
    (
        "nested_code_output_record",
        {
            "extracted_information": [],
            "extract_record_status_info_output": {
                "entity_found": True,
                "entity_name": "Jordan Example",
                "record_number": "1234567890",
                "items": [
                    {
                        "item_name": "Sample Practice",
                        "address": "100 Main St, Example City, ST 12345",
                        "status": "Active",
                    }
                ],
                "overall_status": "Active",
            },
        },
        False,
    ),
    (
        "download_only_files",
        {"downloaded_files": [{"url": "https://example.com/a.pdf", "checksum": "abc123"}]},
        False,
    ),
    (
        "download_only_urls",
        {"extracted_information": None, "downloaded_file_urls": ["https://example.com/a.pdf"]},
        False,
    ),
]


@pytest.mark.parametrize(
    "overrides,expected_empty",
    [(ovr, exp) for _, ovr, exp in _EXTRACTION_ENVELOPE_CASES],
    ids=[case_id for case_id, _, _ in _EXTRACTION_ENVELOPE_CASES],
)
def test_analyze_extraction_envelope(overrides: dict[str, Any], expected_empty: bool) -> None:
    _, empty, _, _ = _analyze_run_blocks(_run_result([_extraction_block(_envelope(**overrides))]))
    assert empty is expected_empty


def test_analyze_text_prompt_default_schema_is_not_empty() -> None:
    # TEXT_PROMPT blocks return the raw LLM response dict (no Task envelope).
    # Default schema is {"llm_response": "<text>"}.
    _, empty, _, _ = _analyze_run_blocks(
        _run_result([_text_prompt_block({"llm_response": "the sentiment is positive"})])
    )
    assert empty is False


def test_analyze_text_prompt_user_schema_named_extracted_information_is_not_sliced() -> None:
    # Guard against a too-broad unwrap: a user's json_schema may name a
    # top-level field "extracted_information". The helper must not mistake
    # that for an EXTRACTION envelope and discard sibling fields.
    block = _text_prompt_block({"extracted_information": "ignored because this is TEXT_PROMPT", "summary": "x"})
    _, empty, _, _ = _analyze_run_blocks(_run_result([block]))
    assert empty is False


def test_analyze_text_prompt_all_null_is_empty() -> None:
    # Symmetric to {"price": None} — a text-prompt response with all-null
    # fields counts as no meaningful output.
    _, empty, _, _ = _analyze_run_blocks(_run_result([_text_prompt_block({"summary": None})]))
    assert empty is True


# ---------------------------------------------------------------------------
# _record_run_blocks_result — end-to-end flip of last_test_ok on empty envelope
# ---------------------------------------------------------------------------


def _fresh_ctx_for_record() -> SimpleNamespace:
    """SimpleNamespace shaped for _record_run_blocks_result + update_repeated_failure_state.

    Mirrors the AgentContext field defaults the function under test reads directly,
    so the stub populates the interesting fields without tripping AttributeError on
    the downstream update_repeated_failure_state call.
    """
    return SimpleNamespace(
        code_artifact_metadata={},
        composition_page_evidence=None,
        block_run_calls_this_turn=0,
        dispatched_run_ids_this_turn=set(),
        unbound_required_parameter_keys=[],
        last_test_ok=True,
        last_test_failure_reason=None,
        last_test_suspicious_success=False,
        last_test_anti_bot=None,
        last_failure_category_top=None,
        last_test_non_retriable_nav_error=None,
        failed_test_nudge_count=0,
        last_failed_workflow_yaml=None,
        last_good_workflow=None,
        last_good_workflow_yaml=None,
        non_retriable_nav_error_last_emitted_signature=None,
        workflow_yaml=None,
        staged_workflow_yaml=None,
        executed_block_labels=set(),
        executed_block_fingerprints={},
        last_workflow=None,
        last_workflow_yaml=None,
        last_frontier_start_label=None,
        last_executed_block_labels=[],
        last_full_workflow_test_ok=False,
        last_unverified_block_labels=[],
        last_failure_signature=None,
        last_frontier_fingerprint=None,
        repeated_failure_streak_count=0,
        repeated_failure_nudge_emitted_at_streak=0,
        pending_action_sequence_fingerprint=None,
        last_action_sequence_fingerprint=None,
        repeated_action_fingerprint_streak_count=0,
        copilot_total_timeout_exceeded=False,
        workflow_verification_evidence=WorkflowVerificationEvidence(),
    )


def test_record_run_blocks_result_records_empty_extraction_without_suspicious_success() -> None:
    ctx = _fresh_ctx_for_record()
    result = _run_result([_extraction_block(_envelope())])
    _record_run_blocks_result(ctx, result)
    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_failure_reason is None
    assert ctx.last_run_outcome.verdict == "not_evaluated"


def test_record_run_blocks_result_does_not_promote_partial_frontier_to_full_workflow() -> None:
    from types import SimpleNamespace

    ctx = _fresh_ctx_for_record()
    ctx.last_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label="open"), SimpleNamespace(label="extract")])
    )
    ctx.last_workflow_yaml = "workflow: yaml"
    ctx.verified_prefix_labels = ["open"]

    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_partial",
            "requested_block_labels": ["open"],
            "executed_block_labels": ["open"],
            "blocks": [{"label": "open", "status": "completed"}],
        },
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is True
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_unverified_block_labels == ["extract"]
    assert ctx.last_good_workflow is None
    assert ctx.last_test_failure_reason is None


def test_record_run_blocks_result_promotes_when_verified_prefix_covers_workflow() -> None:
    from types import SimpleNamespace

    ctx = _fresh_ctx_for_record()
    ctx.last_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label="open"), SimpleNamespace(label="extract")])
    )
    ctx.last_workflow_yaml = "workflow: yaml"
    ctx.verified_prefix_labels = ["open", "extract"]
    ctx.composition_verified_labels = ["open", "extract"]
    ctx.last_unverified_block_labels = ["stale_extract"]

    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_full",
            "requested_block_labels": ["extract"],
            "executed_block_labels": ["extract"],
            "blocks": [{"label": "extract", "status": "completed", "extracted_data": {"value": "ok"}}],
        },
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is True
    assert ctx.last_full_workflow_test_ok is True
    assert ctx.last_unverified_block_labels == []
    assert ctx.last_good_workflow is ctx.last_workflow
    assert ctx.last_good_workflow_yaml == ctx.last_workflow_yaml


def test_record_run_blocks_result_promotes_structured_record_top_level_output_to_terminal_proposal() -> None:
    ctx = _fresh_ctx_for_record()
    ctx.last_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            blocks=[
                SimpleNamespace(label="open_search_search"),
                SimpleNamespace(label="search_and_open_record_details"),
                SimpleNamespace(label="extract_record_status_record"),
            ]
        )
    )
    ctx.last_workflow_yaml = "title: Record lookup"
    ctx.verified_prefix_labels = ["open_search_search"]
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_structured_record",
            "overall_status": "completed",
            "executed_block_labels": ["extract_record_status_record"],
            "blocks": [
                {
                    "label": "extract_record_status_record",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": {"extracted_information": []},
                }
            ],
            "output": {
                "search_and_open_record_details_output": {
                    "found": True,
                    "entity_name": "Jordan Example",
                    "opened_record_details": True,
                    "evidence_text": "Opened Details page for the selected record.",
                },
                "extract_record_status_record_output": {
                    "found": True,
                    "entity_name": "Jordan Example",
                    "record_number": "1234567890",
                    "items": [
                        {
                            "item_label": "Sample Practice",
                            "address": "100 Main St, Example City, ST 12345",
                            "status": "Active",
                        }
                    ],
                    "overall_status": "Active",
                    "evidence_text": "Opened Details page; read Overview/Affiliations items and More Details identifier.",
                },
                "extracted_information": [],
            },
        },
    }
    verification = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=[
            "fallback_record_identity",
            "fallback_record_identifier",
            "fallback_record_groups",
            "fallback_record_status",
        ],
        verdicts=[
            CriterionVerdict(criterion_id=cid, state="satisfied", reason_code="evidence_confirms")
            for cid in (
                "fallback_record_identity",
                "fallback_record_identifier",
                "fallback_record_groups",
                "fallback_record_status",
            )
        ],
    )

    _record_run_blocks_result(ctx, result, completion_verification=verification)

    assert ctx.verified_terminal_proposal_ready is False
    assert ctx.last_test_ok is True
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_failure_reason is None


def test_record_run_blocks_result_resets_stale_verified_terminal_proposal_latch() -> None:
    ctx = _fresh_ctx_for_record()
    ctx.verified_terminal_proposal_ready = True
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_unverified",
            "overall_status": "completed",
            "executed_block_labels": [],
            "blocks": [],
            "output": {},
        },
    }

    _record_run_blocks_result(ctx, result, completion_verification=None)

    assert ctx.verified_terminal_proposal_ready is False


def test_record_run_blocks_result_keeps_failure_when_watchdog_cancel_without_timeout() -> None:
    """Stagnation/ceiling cancels mid-session must still set last_test_ok=False
    so the failed-test nudge can fire — only a coincident total timeout softens
    to ``None`` for the unvalidated WIP rescue path."""
    ctx = _fresh_ctx_for_record()
    result = {
        "ok": False,
        "error": "The run stalled.",
        _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY: True,
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is False
    assert ctx.last_test_failure_reason == "The run stalled."


def test_record_run_blocks_result_sets_last_test_ok_none_on_watchdog_cancel_at_timeout() -> None:
    ctx = _fresh_ctx_for_record()
    ctx.copilot_total_timeout_exceeded = True
    result = {
        "ok": False,
        "error": "Outcome is uncertain.",
        _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY: True,
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is None
    assert ctx.last_test_failure_reason == "Outcome is uncertain."


# ---------------------------------------------------------------------------
# Tool-output pruning
# ---------------------------------------------------------------------------


def _fco(call_id: str, output: str) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _fc(call_id: str) -> dict[str, str]:
    return {"type": "function_call", "call_id": call_id, "name": "evaluate", "arguments": "{}"}


def _history_item(fields: dict[str, Any], *, attr_style: bool) -> dict[str, Any] | SimpleNamespace:
    return SimpleNamespace(**fields) if attr_style else fields


def _tool_history(
    pair_count: int,
    *,
    interleave_screenshots: bool = False,
    attr_style: bool = False,
) -> list[Any]:
    items: list[Any] = [_history_item({"role": "user", "content": "goal"}, attr_style=attr_style)]
    for index in range(pair_count):
        call_id = f"call_{index}"
        items.extend(
            [
                _history_item(_fc(call_id), attr_style=attr_style),
                _history_item(_fco(call_id, "x" * 50), attr_style=attr_style),
            ]
        )
        if interleave_screenshots:
            items.append(
                _history_item(
                    {"role": "user", "content": f"[copilot:screenshot] frame {index}"},
                    attr_style=attr_style,
                )
            )
    return items


def _history_field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _orphaned_tool_result_ids(items: list[Any]) -> list[str]:
    seen_call_ids: set[str] = set()
    orphaned_ids: list[str] = []
    for item in items:
        item_type = _history_field(item, "type")
        call_id = _history_field(item, "call_id")
        if item_type == "function_call" and isinstance(call_id, str):
            seen_call_ids.add(call_id)
        elif item_type == "function_call_output" and call_id not in seen_call_ids:
            orphaned_ids.append(call_id)
    return orphaned_ids


def _call_ids(items: list[Any], item_type: str) -> list[str]:
    return [
        call_id
        for item in items
        if _history_field(item, "type") == item_type and isinstance((call_id := _history_field(item, "call_id")), str)
    ]


def _screenshot_dropped_signals(items: list[Any]) -> list[Any]:
    expected_content = NUDGE_SENTINEL + SCREENSHOT_DROPPED_NUDGE
    return [item for item in items if _history_field(item, "content") == expected_content]


def test_aggressive_prune_drops_orphan_from_eight_pair_repro() -> None:
    pruned = aggressive_prune(_tool_history(8))

    assert _orphaned_tool_result_ids(pruned) == []
    assert _call_ids(pruned, "function_call") == ["call_5", "call_6", "call_7"]
    assert _call_ids(pruned, "function_call_output") == ["call_5", "call_6", "call_7"]


def test_aggressive_prune_emits_one_signal_when_screenshots_are_dropped() -> None:
    opening = {"role": "user", "content": "goal"}
    call = _fc("call_navigate")
    output = _fco("call_navigate", "Navigation complete. A screenshot is attached.")
    screenshots = [
        {"role": "user", "content": "[copilot:screenshot] frame one"},
        {"role": "user", "content": "[copilot:screenshot] frame two"},
    ]

    pruned = aggressive_prune([opening, call, output, *screenshots])

    assert output in pruned
    assert all(not is_screenshot_message(item) for item in pruned)
    assert len(_screenshot_dropped_signals(pruned)) == 1


def test_aggressive_prune_emits_no_signal_without_screenshot_drop() -> None:
    history = _tool_history(3)

    pruned = aggressive_prune(history)

    assert _screenshot_dropped_signals(pruned) == []


def test_aggressive_prune_does_not_duplicate_retained_screenshot_drop_signal() -> None:
    existing_signal = {
        "role": "user",
        "content": NUDGE_SENTINEL + SCREENSHOT_DROPPED_NUDGE,
    }
    history = [
        {"role": "user", "content": "goal"},
        _fc("call_navigate"),
        _fco("call_navigate", "Navigation complete. A screenshot is attached."),
        existing_signal,
        {"role": "user", "content": "[copilot:screenshot] frame"},
    ]

    pruned = aggressive_prune(history)

    assert _screenshot_dropped_signals(pruned) == [existing_signal]


# tail_size samples the boundaries that change behaviour: below one pair, exactly one
# pair, either side of KEEP_RECENT_TOOL_OUTPUTS, the production default, and longer
# than the 21 non-screenshot items _tool_history(10) builds.
@pytest.mark.parametrize("pair_count", [1, 2, 4, 8, 10])
@pytest.mark.parametrize("tail_size", [1, 2, 3, 4, 7, 25])
@pytest.mark.parametrize("interleave_screenshots", [False, True])
@pytest.mark.parametrize("attr_style", [False, True])
def test_aggressive_prune_never_keeps_orphaned_tool_results(
    monkeypatch: pytest.MonkeyPatch,
    pair_count: int,
    tail_size: int,
    interleave_screenshots: bool,
    attr_style: bool,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement._AGGRESSIVE_PRUNE_TAIL", tail_size)
    history = _tool_history(
        pair_count,
        interleave_screenshots=interleave_screenshots,
        attr_style=attr_style,
    )
    original = deepcopy(history)

    pruned = aggressive_prune(history)

    assert _orphaned_tool_result_ids(pruned) == []
    assert history == original
    assert pruned[0] is history[0]
    assert all(not str(_history_field(item, "content") or "").startswith("[copilot:screenshot]") for item in pruned)
    assert len(_screenshot_dropped_signals(pruned)) == int(interleave_screenshots)
    retained_original_items = [item for item in pruned if not _screenshot_dropped_signals([item])]
    retained_indexes = [
        next(index for index, original_item in enumerate(history) if original_item is item)
        for item in retained_original_items
    ]
    assert retained_indexes == sorted(retained_indexes)


def test_aggressive_prune_drops_output_that_precedes_its_call() -> None:
    opening = {"role": "user", "content": "goal"}
    output = _fco("call_late", "result")
    call = _fc("call_late")

    pruned = aggressive_prune([opening, output, call])

    assert pruned == [opening, call]


def test_aggressive_prune_logs_content_free_pair_validity_telemetry() -> None:
    history = _tool_history(8)

    with capture_logs() as logs:
        aggressive_prune(history)

    event = next(entry for entry in logs if entry["event"] == "copilot_aggressive_prune_pair_validity")
    assert event["retained_tail"] == [
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
    ]
    assert event["orphaned_output_dropped"] is True
    assert "call_4" not in json.dumps(event)


def test_copilot_config_qa_budget_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_QA_TOKEN_BUDGET", None)

    assert CopilotConfig().token_budget == 90_000


def test_copilot_config_uses_typed_qa_budget_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    local_settings = Settings(_env_file=None, ENV="local", WORKFLOW_COPILOT_QA_TOKEN_BUDGET=3_000)
    assert local_settings.WORKFLOW_COPILOT_QA_TOKEN_BUDGET == 3_000
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_QA_TOKEN_BUDGET", 3_000)

    assert CopilotConfig().token_budget == 3_000


def test_copilot_config_ignores_qa_budget_in_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_QA_TOKEN_BUDGET", 3_000)

    assert CopilotConfig().token_budget == 90_000


@pytest.mark.asyncio
@pytest.mark.parametrize("tail_size", [1, 2, 3, 4, 7, 25])
@pytest.mark.parametrize("attr_style", [False, True])
async def test_context_overflow_session_rewrite_stores_pair_valid_history(
    monkeypatch: pytest.MonkeyPatch,
    tail_size: int,
    attr_style: bool,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement._AGGRESSIVE_PRUNE_TAIL", tail_size)
    session = AsyncMock()
    session.get_items.return_value = _tool_history(10, interleave_screenshots=True, attr_style=attr_style)

    await _recover_from_context_overflow(session, current_input="continue")

    stored_items = session.add_items.await_args.args[0]
    assert _orphaned_tool_result_ids(stored_items) == []
    session.clear_session.assert_awaited_once()


def test_recent_outputs_preserved_full() -> None:
    # Build KEEP_RECENT_TOOL_OUTPUTS + 1 items so exactly one is "old".
    items = []
    short = '{"ok":true,"data":{"overall_status":"completed"}}'
    for i in range(KEEP_RECENT_TOOL_OUTPUTS + 1):
        items.append(_fco(f"c{i}", short))

    pruned = _prune_input_list(items)
    # Each recent item is unchanged (they're all short and JSON).
    for i in range(1, KEEP_RECENT_TOOL_OUTPUTS + 1):
        assert pruned[i]["output"] == short


def test_recent_code_sized_output_survives_untruncated() -> None:
    # A code-bearing result in the recent window must reach the model whole; the cap
    # is a pathological-payload tripwire, never a ration on legitimate code payloads.
    code_sized = json.dumps({"ok": True, "data": {"code": "await page.click()\n" * 400}})
    assert 2000 < len(code_sized) < _RECENT_TOOL_OUTPUT_CHAR_CAP
    items = [_fco("c0", code_sized)]

    pruned = _prune_input_list(items)
    assert pruned[0]["output"] == code_sized


def test_old_code_output_synopsis_names_elided_code_size() -> None:
    code = "await page.click()\n" * 300
    old_output = json.dumps({"ok": True, "data": {"code": code}})
    items = [_fco("c_old", old_output)] + [_fco(f"c{i}", '{"ok":true}') for i in range(KEEP_RECENT_TOOL_OUTPUTS)]

    pruned = _prune_input_list(items)
    synopsis = json.loads(pruned[0]["output"])
    assert synopsis["code_chars_elided"] == len(code)


def test_old_large_output_is_summarized() -> None:
    # An older, large JSON tool output gets compressed into a synopsis.
    heavy_payload = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_123",
            "overall_status": "completed",
            "blocks": [
                {
                    "label": "open_quote_page",
                    "status": "completed",
                    "block_type": "GOTO_URL",
                    "extracted_data": None,
                },
                {
                    "label": "extract_stock_price",
                    "status": "completed",
                    "block_type": "EXTRACTION",
                    "extracted_data": {"price": None},
                    "failure_reason": None,
                },
            ],
            "visible_elements_html": "<html>" + ("x" * 4000) + "</html>",
            "screenshot_base64": "[base64 image omitted]",
        },
    }
    heavy_output = json.dumps(heavy_payload)
    assert len(heavy_output) > 4000

    items = [_fco("c_old", heavy_output)]
    # Add enough recent outputs to push the first one out of the recent window.
    for i in range(KEEP_RECENT_TOOL_OUTPUTS):
        items.append(_fco(f"c_new_{i}", '{"ok":true,"data":{"overall_status":"completed"}}'))

    pruned = _prune_input_list(items)
    summarized = pruned[0]["output"]
    # The summary must be drastically shorter than the original.
    assert len(summarized) < 1000
    # It must preserve the key signal fields so the agent can still reason about past calls.
    parsed = json.loads(summarized)
    assert parsed["ok"] is True
    assert parsed["overall_status"] == "completed"
    assert parsed["workflow_run_id"] == "wr_123"
    assert parsed["_summarized"]
    assert len(parsed["blocks"]) == 2
    assert parsed["blocks"][1]["label"] == "extract_stock_price"
    assert parsed["blocks"][1]["status"] == "completed"


def test_summarize_non_json_output_falls_back_to_head_truncation() -> None:
    text = "not-json " * 1000
    result = _summarize_tool_output(text)
    assert len(result) < len(text)
    assert result.startswith("not-json")
    assert "older tool output truncated" in result


def test_summarize_short_output_is_unchanged() -> None:
    assert _summarize_tool_output("small") == "small"


def test_recent_large_output_is_head_truncated_not_summarized() -> None:
    import structlog.testing

    # Over-cap JSON in the most-recent slot should be head-truncated,
    # NOT replaced with a summary.
    large = '{"ok":true,"data":{"value":"' + ("y" * (_RECENT_TOOL_OUTPUT_CHAR_CAP + 1000)) + '"}}'
    items = [_fco("c_recent", large)]
    with structlog.testing.capture_logs() as logs:
        pruned = _prune_input_list(items)
    out = pruned[0]["output"]
    assert out.startswith('{"ok":true,')
    assert out.endswith("\n... [truncated]")
    assert len(out) <= _RECENT_TOOL_OUTPUT_CHAR_CAP + 20
    assert any(entry["event"] == "copilot_recent_tool_output_truncated" for entry in logs)


LISTING_DETAIL_URL = "http://localhost:8901/record/1457803926"

# Generic multi-field detail DOM: exercises the contract's label/header binding vs the
# coverage-token channel. No specific vertical or PII (see CLAUDE.md OSS-sync rules).
LISTING_DETAIL_HTML = """
<html><head><title>Regional Records Directory</title></head><body>
<div class="layout">
  <div class="panel">
    <h1>Search Results</h1>
    <p class="muted">Showing 1 result in <strong>Example Region</strong>.</p>
    <div class="result-card" id="recordCard">
      <div>
        <div class="rc-name">Northgate Unit 7</div>
        <div class="muted">Facility</div>
        <div>Northgate Holdings, LLC</div>
        <div class="muted">general listing</div>
        <div class="small">100 Example Ave # 200, Example City, EX 00001</div>
        <div class="small muted">12.34 units away &middot; <a class="link">1-800-555-0102</a></div>
        <div id="recordDetails">
          <div class="kv"><div class="k">Reference Number</div><div>1457803926</div></div>
          <div class="kv"><div class="k">Region</div><div>North</div></div>
          <div class="kv"><div class="k">Category</div><div>Standard</div></div>
          <div class="kv"><div class="k">Tier</div><div>Two</div></div>
          <div class="kv"><div class="k">Effective date</div><div>01/01/2024</div></div>
          <h3>Locations</h3>
          <p class="muted small">Approval status per location for Northgate Holdings, LLC.</p>
          <table>
            <thead><tr><th>Site</th><th>Address</th><th>Status</th></tr></thead>
            <tbody>
              <tr><td>Northgate Holdings, LLC</td><td>100 Example Ave # 200, Example City, EX 00001</td><td><span class="status-ok">Approved</span></td></tr>
              <tr><td>Northgate Holdings, LLC</td><td>240 Sample Blvd, Example City, EX 00002</td><td><span class="status-ok">Approved</span></td></tr>
              <tr><td>Southgate Group</td><td>512 Test St, Other City, EX 00003</td><td><span class="status-no">Not Approved</span></td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="rc-flags"></div>
    </div>
  </div>
  <div class="panel filter-side">
    <h2>Filter Options</h2>
    <div class="fld"><label for="refInput">Search by Name, Group, or Reference Number</label><input id="refInput" type="text"/></div>
    <div class="fld"><label>Reference Number</label><input type="text" value="1457803926"/></div>
  </div>
</div>
</body></html>
"""


def _criterion(output_path: str, outcome: str) -> CompletionCriterion:
    return CompletionCriterion(id=output_path, outcome=outcome, output_path=output_path)


def _registered_download_criterion() -> CompletionCriterion:
    return CompletionCriterion(
        id="output.statement_pdf",
        outcome="the statement PDF is downloaded",
        output_path="output.statement_pdf",
        deliverable_kind="registered_download",
        requested_output_evidence_source="registered_artifact_content",
    )


def _turn_state(*criteria: CompletionCriterion) -> SimpleNamespace:
    return SimpleNamespace(decision=SimpleNamespace(criteria=tuple(criteria)))


def _download_target() -> ReachedDownloadTarget:
    return ReachedDownloadTarget(
        selector="a.download",
        affordance_text="Download",
        download_kind="registered",
        source_step="trajectory_recency",
        already_registered=True,
    )


def _entry_commit_trajectory() -> list[dict[str, object]]:
    return [
        {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Order number"},
        {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
    ]


def test_advisory_run_force_lane_is_deleted() -> None:
    assert not hasattr(enforcement_module, "_should_force_advisory_run_dispatch")


def _deadline_ctx() -> CopilotContext:
    return make_copilot_ctx()


def _deadline_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry.get("event") == "copilot_turn_deadline_expired"]


def test_deadline_fingerprint_carries_elapsed_iteration_and_phase() -> None:
    ctx = _deadline_ctx()

    with capture_logs() as logs:
        _mark_copilot_total_timeout(ctx, elapsed_seconds=901.4567, iteration=12)

    events = _deadline_events(logs)
    assert len(events) == 1
    assert events[0]["elapsed_seconds"] == 901.457
    assert events[0]["iteration"] == 12
    assert ctx.copilot_total_timeout_exceeded is True


def test_deadline_fingerprint_emitted_once_per_turn_across_both_reachable_sites() -> None:
    ctx = _deadline_ctx()

    with capture_logs() as logs:
        _mark_copilot_total_timeout(ctx, elapsed_seconds=901.0, iteration=3)
        _mark_copilot_total_timeout(ctx, elapsed_seconds=902.0, iteration=4)

    assert len(_deadline_events(logs)) == 1
    assert ctx.copilot_total_timeout_exceeded is True


def test_deadline_flag_still_written_when_fingerprint_is_suppressed() -> None:
    ctx = _deadline_ctx()
    ctx.copilot_total_timeout_exceeded = True

    with capture_logs() as logs:
        _mark_copilot_total_timeout(ctx, elapsed_seconds=901.0, iteration=1)

    assert _deadline_events(logs) == []
    assert ctx.copilot_total_timeout_exceeded is True


@pytest.mark.parametrize("iteration", [0, 5, 11])
def test_cancel_site_helper_threads_iteration_into_the_fingerprint(iteration: int) -> None:
    ctx = _deadline_ctx()
    start_time = time.monotonic() - (TOTAL_TIMEOUT_SECONDS + 5.0)

    with capture_logs() as logs:
        _mark_copilot_total_timeout_if_elapsed(ctx, start_time, iteration)

    events = _deadline_events(logs)
    assert len(events) == 1
    assert events[0]["iteration"] == iteration
    assert events[0]["elapsed_seconds"] >= TOTAL_TIMEOUT_SECONDS


def test_cancel_site_helper_is_silent_before_the_deadline() -> None:
    ctx = _deadline_ctx()

    with capture_logs() as logs:
        _mark_copilot_total_timeout_if_elapsed(ctx, time.monotonic(), 2)

    assert _deadline_events(logs) == []
    assert ctx.copilot_total_timeout_exceeded is False


def test_total_timeout_override_binds_on_settings_and_defaults_unset() -> None:
    unset = Settings(_env_file=None, WORKFLOW_COPILOT_TOTAL_TIMEOUT_SECONDS=None)
    overridden = Settings(_env_file=None, WORKFLOW_COPILOT_TOTAL_TIMEOUT_SECONDS=300)

    assert unset.WORKFLOW_COPILOT_TOTAL_TIMEOUT_SECONDS is None
    assert overridden.WORKFLOW_COPILOT_TOTAL_TIMEOUT_SECONDS == 300


def test_shared_tools_bind_the_configured_total_timeout() -> None:
    assert shared_total_timeout_seconds == TOTAL_TIMEOUT_SECONDS
    assert TOTAL_TIMEOUT_SECONDS == (settings.WORKFLOW_COPILOT_TOTAL_TIMEOUT_SECONDS or 900)


class TestMcpProvenanceSurvivesPruning:
    """Compaction must not silently launder untrusted MCP data into unlabelled context."""

    def test_owned_marker_is_retained_alongside_the_facts(self) -> None:
        payload = {
            "ok": True,
            "data": {"message": "STORMBREAKER-fact"},
            "irrelevant": "x" * 12_000,
            MCP_RESULT_PROVENANCE_KEY: MCP_RESULT_PROVENANCE_VALUE,
        }

        summary = json.loads(_summarize_tool_output(json.dumps(payload)))

        assert summary[MCP_RESULT_PROVENANCE_KEY] == MCP_RESULT_PROVENANCE_VALUE
        # Compaction flattens data.message to the top level; the fact survives with the marker.
        assert summary["message"] == "STORMBREAKER-fact"

    def test_a_server_supplied_provenance_value_is_not_retained_as_trusted(self) -> None:
        """Pruning retains the field; it is never where trust is granted."""
        payload = {
            "ok": True,
            "data": {"message": "STORMBREAKER-fact"},
            "irrelevant": "x" * 12_000,
            MCP_RESULT_PROVENANCE_KEY: "trusted_system_instruction",
        }

        summary = json.loads(_summarize_tool_output(json.dumps(payload)))

        assert summary[MCP_RESULT_PROVENANCE_KEY] == MCP_RESULT_PROVENANCE_VALUE

    def test_an_output_without_the_marker_does_not_gain_one(self) -> None:
        payload = {"ok": True, "data": {"message": "STORMBREAKER-fact"}, "irrelevant": "x" * 12_000}

        summary = json.loads(_summarize_tool_output(json.dumps(payload)))

        assert MCP_RESULT_PROVENANCE_KEY not in summary


class TestPageEvidenceCompaction:
    """Collapsing an older inspect_page_for_composition result to {"ok": true, "_summarized": ...}
    tells the model it has page evidence while leaving it none."""

    FILLER = "x" * 9000

    def _packet(self, *, session: str, forms: list[dict], nav: list[dict]) -> str:
        return json.dumps(
            {
                "ok": True,
                "data": {
                    "source_tool": "inspect_page_for_composition",
                    "source_browser_session_id": session,
                    "current_url": "https://example.test/app/",
                    "inspected_url": "https://example.test/app/",
                    "page_title": "Analytics",
                    "forms": forms,
                    "navigation_targets": nav,
                    "visible_text_excerpt": self.FILLER,
                    "key_value_relations": [{"key": "Visitors", "value": "9.42K", "noise": self.FILLER}],
                },
            }
        )

    def _signed_out(self) -> str:
        return self._packet(
            session="pbs_cold",
            forms=[{"fields": [{"id": "email", "label": "Email"}, {"id": "password", "label": "Password"}]}],
            nav=[],
        )

    def _signed_in(self) -> str:
        return self._packet(session="pbs_warm", forms=[], nav=[{"id": "navWebAnalytics", "text": "Web analytics"}])

    def test_page_evidence_is_not_collapsed_to_a_hollow_success(self) -> None:
        compacted = json.loads(_summarize_tool_output(self._signed_out()))

        assert compacted.keys() != {"ok", "_summarized"}, "an observation became a success placeholder"
        assert "page_evidence" in compacted

    def test_two_browser_states_keep_their_own_facts_and_provenance(self) -> None:
        cold = json.loads(_summarize_tool_output(self._signed_out()))["page_evidence"]
        warm = json.loads(_summarize_tool_output(self._signed_in()))["page_evidence"]

        assert cold["source_browser_session_id"] == "pbs_cold"
        assert warm["source_browser_session_id"] == "pbs_warm"
        assert cold["current_url"] == warm["current_url"], "same URL — only the browser state differs"
        assert "email" in json.dumps(cold["forms"]), "the sign-in fields are what the cold state carries"
        assert not warm.get("forms")
        assert "navWebAnalytics" in json.dumps(warm["navigation_targets"])

    def test_raw_excerpts_are_dropped_before_facts(self) -> None:
        compacted = _summarize_tool_output(self._signed_out())

        assert self.FILLER not in compacted, "raw excerpts and derived relations go first"
        assert len(compacted) < len(self._signed_out()) / 4

    def test_a_crowded_page_cannot_defeat_compaction(self) -> None:
        """Per-list bounds alone would let a page with many forms and links compact to roughly the
        size it arrived at, which is the opposite of what this pass is for."""
        lists = {
            key: [{"id": f"{key}-{index}", "text": "y" * 380} for index in range(12)]
            for key in (
                "forms",
                "navigation_targets",
                "result_containers",
                "clickable_controls",
                "challenge_controls",
                "modal_overlays",
                "page_obstructions",
            )
        }
        crowded = json.dumps(
            {
                "ok": True,
                "data": {
                    "source_tool": "inspect_page_for_composition",
                    "inspected_url": "https://example.test/app/",
                    **lists,
                },
            }
        )

        compacted = _summarize_tool_output(crowded)

        assert len(compacted) < 5000, f"compacted output was {len(compacted)} chars"
        assert json.loads(compacted)["page_evidence"]["forms"], "the first facts still survive"


def test_the_run_packets_typed_failure_facts_survive_compaction() -> None:
    # Retention is an allowlist, so a field added after it was written is dropped in silence. A
    # repair conversation of any length outlives three tool outputs.
    payload = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_cold",
            "overall_status": "failed",
            "failing_code_line": 6,
            "blocks": [
                {
                    "label": "extract_failure_rate",
                    "status": "failed",
                    "failure_reason": "code error at line 6",
                    "error_codes": ["user_code_error"],
                }
            ],
            "build_test_packet": {
                "contract_version": "build_test_evidence_packet_v1",
                "run": {"workflow_run_id": "wr_cold", "status": "failed"},
                "failure": {
                    "block_label": "extract_failure_rate",
                    "error_codes": ["user_code_error"],
                    "failing_line": 6,
                    "locator_observations": [{"authored_selector": "x", "match_count": 0}] * 40,
                },
            },
            "page_text": "x" * 6000,
        },
    }

    parsed = json.loads(_summarize_tool_output(json.dumps(payload)))

    assert parsed["failing_code_line"] == 6
    assert parsed["blocks"][0]["error_codes"] == ["user_code_error"]
    retained = parsed["build_test_packet"]
    assert retained["run"]["workflow_run_id"] == "wr_cold"
    assert retained["failure"]["error_codes"] == ["user_code_error"]
    assert retained["failure"]["failing_line"] == 6
    # The list tail is what yields, not the scalars a repair acts on.
    assert "locator_observations" not in retained["failure"]


def test_a_compacted_output_without_a_packet_gains_no_empty_one() -> None:
    payload = {"ok": True, "data": {"workflow_run_id": "wr_1", "overall_status": "completed", "pad": "y" * 6000}}

    parsed = json.loads(_summarize_tool_output(json.dumps(payload)))

    assert "build_test_packet" not in parsed
    assert "failing_code_line" not in parsed

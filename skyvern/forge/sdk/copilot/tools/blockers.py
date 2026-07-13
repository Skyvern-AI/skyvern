from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import structlog

from skyvern.forge import app
from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.sdk.api.llm.schema_validator import validate_and_fill_extraction_result
from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
    assert_clean_user_facing_text,
    build_loop_blocker_signal,
    loop_blocker_evidence_from_ctx,
    refresh_held_loop_blocker_evidence,
)
from skyvern.forge.sdk.copilot.build_test_outcome import (
    maybe_satisfy_recorded_outcome_grounding_requirement,
    recorded_outcome_grounding_requires_current_page,
)
from skyvern.forge.sdk.copilot.challenge_evidence import (
    artifact_challenge_flag_key,
    is_carrier_backed_category_entry,
)
from skyvern.forge.sdk.copilot.completion_verification import (
    structured_record_has_goal_content as _structured_record_candidate_has_goal_content,
)
from skyvern.forge.sdk.copilot.composition_evidence import interactive_challenge_controls
from skyvern.forge.sdk.copilot.enforcement import (
    TOTAL_TIMEOUT_SECONDS,
    synthesized_block_persistence_signal,
    terminal_challenge_blocker_signal_from_current_page_evidence,
    uncovered_output_reject_scout_steer_signal,
)
from skyvern.forge.sdk.copilot.failure_tracking import (
    ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY,
    ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE,
    PER_TOOL_BUDGET_FAILURE_CATEGORY,
)
from skyvern.forge.sdk.copilot.loop_detection import detect_failed_tool_step_loop_for_ctx, detect_tool_loop
from skyvern.forge.sdk.copilot.reached_download_target import REGISTERED_DOWNLOAD_OUTPUT_KEYS
from skyvern.forge.sdk.copilot.run_outcome import trusted_terminal_challenge_category_name
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    AuthorTimeGateAblationPayload,
    output_contract_ladder_unresolved,
    record_author_time_gate_ablation_event,
)
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.schemas.workflows import BlockType

from ._shared import (
    _CONSECUTIVE_LOOP_GUARD_EXEMPT_TOOLS,
    _DATA_PRODUCING_BLOCK_TYPES,
    _FAILED_BLOCK_STATUSES,
    BLOCK_RUNNING_TOOLS,
    COPILOT_FINAL_REPLY_RESERVE_SECONDS,
    PAGE_INSPECTION_TOOLS,
    PAGE_SCHEMA_CONTEXT_TOOLS,
    PER_TOOL_CALL_BUDGET_SECONDS,
    _block_data_payload,
    _block_label_from_yaml,
    _copilot_seconds_remaining,
    _current_workflow_block_labels,
    _emit_tool_blocker_signal,
    _enum_or_string_name,
    _is_meaningful_extracted_data,
    _parse_workflow_blocks,
    _raw_yaml_proxy_location,
    _registered_output_parameter_payloads,
    _workflow_output_parameter_payloads,
)

LOG = structlog.get_logger()

_CURRENT_PAGE_TERMINAL_CHALLENGE_TOOLS = (
    BLOCK_RUNNING_TOOLS
    | PAGE_INSPECTION_TOOLS
    | frozenset({"click", "navigate_browser", "press_key", "scroll", "select_option", "type_text"})
)
_OUTPUT_CONTRACT_LADDER_AUTHORING_TOOLS = frozenset({"update_workflow", "update_and_run_blocks"})
_RECORDED_OUTCOME_GROUNDING_MUTATION_TOOLS = BLOCK_RUNNING_TOOLS | frozenset(
    {
        "update_workflow",
        "click",
        "fill_credential_field",
        "navigate_browser",
        "press_key",
        "scroll",
        "select_option",
        "type_text",
    }
)


async def _safe_read_workflow_run(
    workflow_run_id: str,
    organization_id: str,
    *,
    context: str,
) -> WorkflowRun | None:
    """Read a workflow_runs row, logging-and-returning-None on failure.

    The ``context`` string distinguishes call sites in logs (e.g.
    ``"pre-cancel"`` vs ``"post-drain"``) so a failure is attributable to
    the specific phase of the timeout branch it fired from.
    """
    try:
        return await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
    except Exception:
        LOG.warning(
            "Workflow run re-read failed",
            workflow_run_id=workflow_run_id,
            context=context,
            exc_info=True,
        )
        return None


def _trusted_post_drain_status(run: WorkflowRun | None) -> str | None:
    """Return the run's status if it is one we can trust after the cancel
    helper has run; otherwise ``None``.

    ``canceled`` is deliberately rejected because at post-drain read time we
    can't tell a legitimate ``canceled`` (written by
    ``_finalize_workflow_run_status`` when a block/user canceled the run)
    apart from a synthetic ``canceled`` (written by the cancel helper's
    fallback). Callers that need to distinguish those cases must read the row
    BEFORE the cancel helper runs.
    """
    if run is None:
        return None
    if WorkflowRunStatus(run.status).is_final_excluding_canceled():
        return run.status
    return None


def _active_run_terminal_evidence_detected(result: Mapping[str, object]) -> bool:
    data_value = result.get("data")
    data = data_value if isinstance(data_value, Mapping) else {}
    return data.get("active_run_terminal_evidence_detected") is True


def _active_run_terminal_evidence_signal(ctx: AgentContext, tool_name: str) -> CopilotToolBlockerSignal | None:
    evidence = getattr(ctx, "workflow_verification_evidence", None)
    has_active_terminal_evidence = getattr(
        ctx, "last_failure_category_top", None
    ) == ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY or bool(
        getattr(evidence, "active_run_terminal_evidence_detected", False)
    )
    if not has_active_terminal_evidence:
        return None

    run_id = getattr(evidence, "active_run_terminal_evidence_workflow_run_id", None) or getattr(
        ctx, "last_run_blocks_workflow_run_id", None
    )
    location = (
        getattr(evidence, "page_title", None) or getattr(evidence, "current_url", None) or "the current browser page"
    )
    run_detail = f" Workflow run: {run_id}." if isinstance(run_id, str) and run_id else ""
    agent_steering = (
        "The prior active workflow run emitted typed ACTIVE_RUN_TERMINAL_EVIDENCE while the browser task "
        f"was still running.{run_detail} The current page evidence already matched the user's terminal "
        "browser-state criteria, but the reusable workflow is not verified end-to-end. "
        f"Do not call {tool_name} again in this turn; reply with a partial-verification/blocker state that "
        "says the requested browser state was observed, the active run was interrupted before overshoot, "
        "and the workflow still needs a clean corrected run before it can be offered as tested."
    )
    user_facing = (
        f"I reached the requested browser state on {location} and stopped before continuing, "
        "but the reusable workflow still needs a clean verification run before it is ready."
    )
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=agent_steering,
        user_facing_reason=user_facing,
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code=ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE,
        blocked_tool=tool_name,
    )


def _per_tool_budget_problem_label_set(ctx: Any) -> set[str]:
    labels = getattr(ctx, "per_tool_budget_problem_block_labels", None)
    if not isinstance(labels, list):
        return set()
    return {label for label in labels if isinstance(label, str) and label}


def _record_per_tool_budget_problem_blocks_from_results(copilot_ctx: Any, result: dict[str, Any]) -> None:
    if getattr(copilot_ctx, "last_failure_category_top", None) != PER_TOOL_BUDGET_FAILURE_CATEGORY:
        return
    data = result.get("data")
    if not isinstance(data, dict):
        return
    pending_run_id = getattr(copilot_ctx, "pending_reconciliation_run_id", None)
    resolved_run_id = data.get("workflow_run_id")
    if isinstance(pending_run_id, str) and pending_run_id and resolved_run_id != pending_run_id:
        return
    blocks = data.get("blocks")
    if not isinstance(blocks, list):
        return

    labels = _per_tool_budget_problem_label_set(copilot_ctx)
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if _enum_or_string_name(block.get("block_type")) != BlockType.NAVIGATION.value:
            continue
        if _enum_or_string_name(block.get("status")) not in _FAILED_BLOCK_STATUSES:
            continue
        label = block.get("label")
        if isinstance(label, str) and label:
            labels.add(label)
    copilot_ctx.per_tool_budget_problem_block_labels = sorted(labels)


def _navigation_labels_in_workflow(workflow: Any) -> set[str]:
    definition = getattr(workflow, "workflow_definition", None)
    blocks = getattr(definition, "blocks", None)
    if not isinstance(blocks, list):
        return set()

    labels: set[str] = set()
    for block in blocks:
        if _enum_or_string_name(getattr(block, "block_type", None)) != BlockType.NAVIGATION.value:
            continue
        label = getattr(block, "label", None)
        if isinstance(label, str) and label:
            labels.add(label)
    return labels


def _clear_resolved_per_tool_budget_problem_labels(copilot_ctx: Any, workflow: Any) -> None:
    problem_labels = _per_tool_budget_problem_label_set(copilot_ctx)
    if not problem_labels:
        return
    remaining = sorted(problem_labels & _navigation_labels_in_workflow(workflow))
    copilot_ctx.per_tool_budget_problem_block_labels = remaining


def _requested_block_label_set(arguments: dict[str, Any] | None) -> set[str] | None:
    if not isinstance(arguments, dict):
        return None
    block_labels = arguments.get("block_labels")
    if not isinstance(block_labels, list):
        return None
    return {label for label in block_labels if isinstance(label, str) and label}


def _per_tool_budget_problem_rerun_signal(
    ctx: Any, arguments: dict[str, Any] | None, tool_name: str
) -> CopilotToolBlockerSignal | None:
    problem_labels = _per_tool_budget_problem_label_set(ctx)
    if not problem_labels:
        return None

    requested_labels = _requested_block_label_set(arguments)
    blocked_labels = problem_labels if not requested_labels else problem_labels & requested_labels
    if not blocked_labels:
        return None

    labels = ", ".join(sorted(blocked_labels))
    agent_steering = (
        "The prior PER_TOOL_BUDGET run's get_run_results showed navigation "
        f"block(s) [{labels}] were canceled or failed while applying page state. "
        f"Do NOT rerun those block label(s) unchanged with {tool_name}. "
        "Update the workflow to split or replace the oversized navigation block first, "
        "use live-page inspection evidence to decide what state is actually missing, "
        "or run only newly-created smaller block labels that apply one missing constraint at a time."
    )
    user_facing = (
        "The previous run hit the per-step time budget before I could finish. "
        "I'll continue from the verified browser state instead of retrying blindly."
    )
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=agent_steering,
        user_facing_reason=user_facing,
        recovery_hint="retry_with_different_tool",
        cleared_by_tools=PAGE_INSPECTION_TOOLS
        | frozenset({"get_run_results", "update_workflow", "update_and_run_blocks"}),
        internal_reason_code="tool_error_per_tool_budget_rerun",
        blocked_tool=tool_name,
    )


def _workflow_yaml_ordered_labels(workflow_yaml: str | None) -> list[str]:
    blocks = _parse_workflow_blocks(workflow_yaml)
    if not blocks:
        return []
    labels: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        label = _block_label_from_yaml(block)
        if label:
            labels.append(label)
    return labels


def _post_budget_upstream_replay_signal(
    ctx: AgentContext, arguments: dict[str, Any] | None, tool_name: str
) -> CopilotToolBlockerSignal | None:
    problem_labels = _per_tool_budget_problem_label_set(ctx)
    if not problem_labels:
        return None

    evidence = getattr(ctx, "composition_page_evidence", None)
    if not isinstance(evidence, dict) or evidence.get("observed_after_workflow_run") is not True:
        return None

    requested_labels = _requested_block_label_set(arguments)
    if not requested_labels:
        return None

    workflow_yaml = arguments.get("workflow_yaml") if isinstance(arguments, dict) else None
    ordered_labels = _workflow_yaml_ordered_labels(workflow_yaml if isinstance(workflow_yaml, str) else None)
    if not ordered_labels:
        ordered_labels = _current_workflow_block_labels(ctx)
    if not ordered_labels:
        return None

    first_problem_index = min(
        (ordered_labels.index(label) for label in problem_labels if label in ordered_labels),
        default=None,
    )
    if first_problem_index is None:
        return None

    upstream_requested = [label for label in ordered_labels[:first_problem_index] if label in requested_labels]
    if not upstream_requested:
        return None

    upstream = ", ".join(upstream_requested)
    frontier = ", ".join(sorted(problem_labels))
    agent_steering = (
        "The prior PER_TOOL_BUDGET run advanced the live browser, and you already inspected that "
        "post-run page state. Do NOT restart upstream label(s) "
        f"[{upstream}] before the budgeted frontier [{frontier}]. "
        "Answer from the observed page if it contains the requested result/no-result evidence. "
        "If more workflow testing is still required, preserve the verified upstream blocks and run only "
        "new or modified smaller labels at/after the missing frontier."
    )
    user_facing = (
        "The previous run already reached a later browser state. I'll continue from that evidence "
        "instead of restarting earlier steps."
    )
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=agent_steering,
        user_facing_reason=user_facing,
        recovery_hint="retry_with_different_tool",
        cleared_by_tools=PAGE_INSPECTION_TOOLS
        | frozenset({"get_run_results", "update_workflow", "update_and_run_blocks"}),
        preserves_workflow_draft=True,
        renders_final_reply=False,
        internal_reason_code="tool_error_post_budget_upstream_replay",
        blocked_tool=tool_name,
    )


def _composition_control_label(control: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("text", "name", "id", "selector"):
        value = control.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    unique = list(dict.fromkeys(parts))
    return " / ".join(unique[:2])[:160] if unique else "submit/search control"


def _disabled_submit_controls_from_evidence(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    forms = evidence.get("forms")
    if not isinstance(forms, list):
        return controls
    for form in forms:
        if not isinstance(form, dict):
            continue
        for control in form.get("submit_controls") or []:
            if isinstance(control, dict) and control.get("disabled") is True:
                controls.append(control)
    return controls[:5]


def _post_run_terminal_challenge_reason(evidence: dict[str, Any]) -> str | None:
    if evidence.get("observed_after_workflow_run") is not True:
        return None

    challenge_state = evidence.get("challenge_state")
    if isinstance(challenge_state, dict):
        gated_controls = [
            control for control in challenge_state.get("gated_submit_controls") or [] if isinstance(control, dict)
        ]
        if challenge_state.get("gates_submit_controls") is True:
            kind = str(challenge_state.get("kind") or "anti-bot challenge").replace("_", " ")
            labels = ", ".join(_composition_control_label(control) for control in gated_controls[:3])
            controls_text = f" ({labels})" if labels else ""
            return f"{kind} is still gating disabled submit/search control(s){controls_text}"
        if challenge_state.get("detected") is True and challenge_state.get("requires_human_verification") is True:
            disabled_controls = _disabled_submit_controls_from_evidence(evidence)
            if disabled_controls:
                labels = ", ".join(_composition_control_label(control) for control in disabled_controls[:3])
                return f"human-verification challenge remains while submit/search control(s) are disabled ({labels})"

    indicators = evidence.get("anti_bot_indicators")
    has_indicators = isinstance(indicators, list) and any(isinstance(item, str) and item.strip() for item in indicators)
    # Raw-HTML token hits alone never justify a terminal claim; a rendered
    # challenge control must corroborate them.
    if has_indicators and interactive_challenge_controls(evidence.get("challenge_controls")):
        disabled_controls = _disabled_submit_controls_from_evidence(evidence)
        if disabled_controls:
            labels = ", ".join(_composition_control_label(control) for control in disabled_controls[:3])
            return f"anti-bot evidence remains while submit/search control(s) are disabled ({labels})"
    return None


def _post_run_result_evidence_summary(evidence: dict[str, Any]) -> str | None:
    containers = [container for container in evidence.get("result_containers") or [] if isinstance(container, dict)]
    if not containers:
        return None
    hints: list[str] = []
    for container in containers[:3]:
        for key in ("selector", "id", "tag"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                hints.append(value.strip()[:80])
                break
    hint_text = f" ({', '.join(hints)})" if hints else ""
    return f"{len(containers)} result container(s){hint_text}"


_POST_BUDGET_CHALLENGE_RESULT_EVIDENCE_REASON = "tool_error_post_budget_challenge_result_evidence"
_POST_BUDGET_CHALLENGE_BLOCKER_REASON = "tool_error_post_budget_challenge_blocker"
_CHALLENGE_STOP_TERMS = frozenset(
    {
        "anti bot",
        "anti-bot",
        "captcha",
        "challenge",
        "disabled search",
        "disabled submit",
        "human verification",
        "turnstile",
        "verification",
    }
)
_VISIBLE_TEXT_CHROME_TOKENS = frozenset(
    {
        "and",
        "are",
        "bot",
        "button",
        "captcha",
        "challenge",
        "clear",
        "control",
        "disabled",
        "enter",
        "first",
        "form",
        "full",
        "human",
        "last",
        "lookup",
        "name",
        "reset",
        "search",
        "select",
        "state",
        "submit",
        "verification",
        "verify",
        "you",
        "your",
    }
)
_RESULT_CONTAINER_CHROME_TOKENS = _VISIBLE_TEXT_CHROME_TOKENS | frozenset(
    {"found", "matching", "record", "records", "result", "results"}
)


def _safe_signature_text(value: Any, max_chars: int = 180) -> str:
    text = "" if value is None else str(value).strip()
    return text[:max_chars]


def _signature_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _diagnosis_stop_has_challenge_evidence(ctx: Any, reason: str) -> bool:
    contract = getattr(ctx, "latest_diagnosis_repair_contract", None)
    repair_decision = getattr(contract, "repair_decision", None)
    if _enum_or_string_name(getattr(repair_decision, "next_action", None)) != "stop":
        return False

    diagnosis_input = getattr(contract, "diagnosis_input", None)
    diagnosis_result = getattr(contract, "diagnosis_result", None)
    verification_result = getattr(contract, "verification_result", None)
    categories = getattr(diagnosis_input, "failure_categories", None)
    category_text = (
        " ".join(str(category) for category in categories if category) if isinstance(categories, list) else ""
    )
    haystack = " ".join(
        part
        for part in (
            reason,
            category_text,
            _enum_or_string_name(getattr(diagnosis_result, "suspected_failure_type", None)),
            getattr(diagnosis_result, "root_cause_summary", None),
            getattr(verification_result, "remaining_blocker", None),
            getattr(ctx, "last_test_anti_bot", None),
        )
        if isinstance(part, str) and part
    )
    normalized = haystack.replace("_", " ").lower()
    return any(term in normalized for term in _CHALLENGE_STOP_TERMS)


def _challenge_control_signature(control: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": _composition_control_label(control),
        "disabled": control.get("disabled") is True,
    }


def _post_budget_challenge_signature(evidence: dict[str, Any], reason: str) -> str:
    challenge_state = evidence.get("challenge_state")
    challenge = challenge_state if isinstance(challenge_state, dict) else {}
    gated_controls = [
        _challenge_control_signature(control)
        for control in challenge.get("gated_submit_controls") or []
        if isinstance(control, dict)
    ]
    disabled_controls = [
        _challenge_control_signature(control) for control in _disabled_submit_controls_from_evidence(evidence)
    ]
    return _signature_json(
        {
            "reason": reason,
            "challenge": {
                "detected": challenge.get("detected") is True,
                "gates_submit_controls": challenge.get("gates_submit_controls") is True,
                "kind": _safe_signature_text(challenge.get("kind"), 80),
                "requires_human_verification": challenge.get("requires_human_verification") is True,
            },
            "disabled_submit_controls": sorted(disabled_controls, key=lambda item: item["label"]),
            "gated_submit_controls": sorted(gated_controls, key=lambda item: item["label"]),
        }
    )


def _post_budget_result_evidence_signature(evidence: dict[str, Any], result_evidence: str) -> str:
    containers = [container for container in evidence.get("result_containers") or [] if isinstance(container, dict)]
    normalized_containers: list[dict[str, Any]] = []
    for container in containers[:5]:
        normalized_containers.append(
            {
                "id": _safe_signature_text(container.get("id"), 120),
                "row_count": container.get("row_count") if isinstance(container.get("row_count"), int) else None,
                "selector": _safe_signature_text(container.get("selector"), 160),
                "tag": _safe_signature_text(container.get("tag"), 40),
            }
        )
    return _signature_json({"containers": normalized_containers, "summary": result_evidence})


def _result_container_has_content(container: dict[str, Any]) -> bool:
    row_count = container.get("row_count")
    if isinstance(row_count, int) and row_count > 0:
        return True
    for key in ("sample_rows", "rows", "items"):
        value = container.get(key)
        if isinstance(value, list) and _is_meaningful_extracted_data(value):
            return True
    for key in ("content_excerpt", "sample_text", "text", "text_excerpt", "visible_results_evidence"):
        value = container.get(key)
        if isinstance(value, str):
            tokens = {token for token in re.findall(r"[a-z0-9]{2,}", value.lower()) if token}
            if tokens - set(_RESULT_CONTAINER_CHROME_TOKENS):
                return True
            continue
        if value:
            return True
    return False


def _visible_text_excerpt_has_result_content(evidence: dict[str, Any]) -> bool:
    text = evidence.get("visible_text_excerpt")
    if not isinstance(text, str) or not text.strip():
        return False
    tokens = {token for token in re.findall(r"[a-z0-9]{2,}", text.lower()) if token}
    if not tokens:
        return False
    control_tokens: set[str] = set(_VISIBLE_TEXT_CHROME_TOKENS)
    for form in evidence.get("forms") or []:
        if not isinstance(form, dict):
            continue
        for field in form.get("fields") or []:
            if not isinstance(field, dict):
                continue
            for key in ("label", "name", "placeholder", "value"):
                value = field.get(key)
                if isinstance(value, str):
                    control_tokens.update(re.findall(r"[a-z0-9]{2,}", value.lower()))
            for option in field.get("options") or []:
                if isinstance(option, dict):
                    for key in ("text", "value"):
                        value = option.get(key)
                        if isinstance(value, str):
                            control_tokens.update(re.findall(r"[a-z0-9]{2,}", value.lower()))
    for control in _disabled_submit_controls_from_evidence(evidence):
        control_tokens.update(re.findall(r"[a-z0-9]{2,}", _composition_control_label(control).lower()))
    challenge_state = evidence.get("challenge_state")
    if isinstance(challenge_state, dict):
        control_tokens.update(re.findall(r"[a-z0-9]{2,}", str(challenge_state.get("kind") or "").lower()))
        for indicator in challenge_state.get("indicators") or []:
            if isinstance(indicator, str):
                control_tokens.update(re.findall(r"[a-z0-9]{2,}", indicator.lower()))
    for indicator in evidence.get("anti_bot_indicators") or []:
        if isinstance(indicator, str):
            control_tokens.update(re.findall(r"[a-z0-9]{2,}", indicator.lower()))
    return bool(tokens - control_tokens)


def _post_run_result_evidence_has_content(evidence: dict[str, Any]) -> bool:
    containers = [container for container in evidence.get("result_containers") or [] if isinstance(container, dict)]
    if any(_result_container_has_content(container) for container in containers):
        return True
    if containers and _visible_text_excerpt_has_result_content(evidence):
        return True
    for key in ("visible_results_evidence", "result_text_excerpt"):
        value = evidence.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _has_prior_matching_challenge_result_evidence(
    ctx: Any, *, challenge_signature: str, result_evidence_signature: str
) -> bool:
    history = getattr(ctx, "tool_blocker_signals", None)
    if not isinstance(history, list):
        return False
    for signal in reversed(history):
        if not isinstance(signal, CopilotToolBlockerSignal):
            continue
        if signal.internal_reason_code != _POST_BUDGET_CHALLENGE_RESULT_EVIDENCE_REASON:
            continue
        extra = signal.extra
        if (
            extra.get("challenge_signature") == challenge_signature
            and extra.get("result_evidence_signature") == result_evidence_signature
            and extra.get("result_evidence_populated") is not True
        ):
            return True
    return False


def _terminal_challenge_signal(
    *, reason: str, arguments: dict[str, Any] | None, tool_name: str, extra: dict[str, Any] | None = None
) -> CopilotToolBlockerSignal:
    requested_labels = _requested_block_label_set(arguments)
    labels_text = f" Requested labels: {', '.join(sorted(requested_labels))}." if requested_labels else ""
    agent_steering = (
        "The prior block-running tool hit a failed/budgeted frontier, and bounded current-page inspection "
        f"now shows: {reason}.{labels_text} Do NOT call "
        f"{tool_name} again in this turn, do NOT try another proxy/location from this evidence state, and "
        "do NOT claim results or no-results were verified. REPLY now with a blocker explanation "
        "that names the observed challenge/disabled control and summarizes the tested workflow state."
    )
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=agent_steering,
        user_facing_reason=_terminal_challenge_user_facing_reason(reason),
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code=_POST_BUDGET_CHALLENGE_BLOCKER_REASON,
        blocked_tool=tool_name,
        extra=extra or {},
    )


_TERMINAL_CHALLENGE_FALLBACK_USER_FACING = (
    "A page barrier was still blocking the workflow's next step after the last run, "
    "so I stopped without claiming results."
)


def _terminal_challenge_user_facing_reason(reason: str) -> str:
    # The narrative names the observed evidence (challenge kind, control labels)
    # instead of asserting a fixed mechanism; page-derived text falls back to a
    # pre-validated template if it ever trips the leak deny list.
    candidate = (
        f"I stopped because the page I inspected after the last run still shows: {reason}. "
        "I haven't verified any results, so I'm not claiming them."
    )
    try:
        assert_clean_user_facing_text(candidate)
    except ValueError:
        return _TERMINAL_CHALLENGE_FALLBACK_USER_FACING
    return candidate


def _post_budget_terminal_challenge_signal(
    ctx: AgentContext, arguments: dict[str, Any] | None, tool_name: str
) -> CopilotToolBlockerSignal | None:
    evidence = getattr(ctx, "composition_page_evidence", None)
    if not isinstance(evidence, dict):
        return None

    # A failed run with post-run page evidence can prove the same terminal
    # challenge state even when the top-level failure category was not budget.
    prior_budget_or_failed_run = (
        getattr(ctx, "last_failure_category_top", None) == PER_TOOL_BUDGET_FAILURE_CATEGORY
        or getattr(ctx, "last_test_ok", None) is False
        or getattr(ctx, "post_run_page_observation_after_failed_test", False) is True
        or bool(_per_tool_budget_problem_label_set(ctx))
    )
    if not prior_budget_or_failed_run:
        return None

    reason = _post_run_terminal_challenge_reason(evidence)
    if not reason:
        return None

    # Same-packet adjudication: when the observed page also carries result
    # evidence, the answer-from-observed-page path outranks a terminal claim.
    result_evidence = _post_run_result_evidence_summary(evidence)
    if result_evidence:
        challenge_signature = _post_budget_challenge_signature(evidence, reason)
        result_evidence_signature = _post_budget_result_evidence_signature(evidence, result_evidence)
        result_evidence_populated = _post_run_result_evidence_has_content(evidence)
        extra = {
            "challenge_signature": challenge_signature,
            "result_evidence_populated": result_evidence_populated,
            "result_evidence_signature": result_evidence_signature,
        }
        if (
            not result_evidence_populated
            and _diagnosis_stop_has_challenge_evidence(ctx, reason)
            and _has_prior_matching_challenge_result_evidence(
                ctx,
                challenge_signature=challenge_signature,
                result_evidence_signature=result_evidence_signature,
            )
        ):
            return _terminal_challenge_signal(
                reason=reason,
                arguments=arguments,
                tool_name=tool_name,
                extra={
                    **extra,
                    "escalated_from_reason_code": _POST_BUDGET_CHALLENGE_RESULT_EVIDENCE_REASON,
                },
            )

        agent_steering = (
            "The prior block-running tool hit a failed/budgeted frontier, and bounded current-page inspection "
            f"shows: {reason} — but the SAME observed page also contains result evidence: {result_evidence}. "
            f"Do NOT claim the page is blocked and do NOT rerun {tool_name} unchanged. "
            "Answer from the observed page if it contains the requested result/no-result evidence; "
            "otherwise inspect the live page again before deciding."
        )
        return CopilotToolBlockerSignal(
            blocker_kind="tool_error",
            agent_steering_text=agent_steering,
            user_facing_reason=(
                "The last page I checked already shows results. I'll work from that evidence instead of retrying."
            ),
            recovery_hint="retry_with_different_tool",
            cleared_by_tools=PAGE_INSPECTION_TOOLS
            | frozenset({"get_run_results", "update_workflow", "update_and_run_blocks"}),
            preserves_workflow_draft=True,
            renders_final_reply=False,
            internal_reason_code=_POST_BUDGET_CHALLENGE_RESULT_EVIDENCE_REASON,
            blocked_tool=tool_name,
            extra=extra,
        )

    return _terminal_challenge_signal(reason=reason, arguments=arguments, tool_name=tool_name)


def _current_page_terminal_challenge_signal(
    ctx: AgentContext, arguments: dict[str, Any] | None, tool_name: str
) -> CopilotToolBlockerSignal | None:
    signal = terminal_challenge_blocker_signal_from_current_page_evidence(
        ctx,
        blocked_tool=tool_name,
        evidence_source="page_evidence",
    )
    if signal is None:
        return None
    requested_labels = _requested_block_label_set(arguments)
    if requested_labels:
        signal = signal.model_copy(update={"extra": {**dict(signal.extra), "block_labels": sorted(requested_labels)}})
    return signal


_RECONCILIATION_REQUIRES_INPUT_USER_FACING = (
    "The previous run was canceled. Tell me whether to retry, keep the draft as-is, or adjust the workflow first."
)
_RECONCILIATION_NO_INPUT_USER_FACING = (
    "The previous run ended without a verified result. I'll check what happened before doing anything else this turn."
)


def _pending_reconciliation_requires_input_signal(
    *, pending_run_id: str, blocked_tool: str
) -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=(
            f"The canceled run {pending_run_id} has already been inspected. "
            f"Do NOT run more blocks in this turn; ask the user whether to retry, "
            f"accept the unverified draft, or adjust the workflow first. This guard "
            f"prevents duplicate side effects on live sites."
        ),
        user_facing_reason=_RECONCILIATION_REQUIRES_INPUT_USER_FACING,
        recovery_hint="ask_user_clarifying",
        cleared_by_tools=frozenset(),
        internal_reason_code="tool_error_pending_reconciliation_requires_input",
        blocked_tool=blocked_tool,
    )


def _pending_reconciliation_no_input_signal(*, pending_run_id: str, blocked_tool: str) -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=(
            f"The previous block-running tool call for run {pending_run_id} "
            f"ended without a trustworthy terminal status. "
            f'Call `get_run_results(workflow_run_id="{pending_run_id}")` '
            f"first, report the result to the user, then await user input "
            f"before running more blocks. This guard prevents duplicate "
            f"side effects on live sites."
        ),
        user_facing_reason=_RECONCILIATION_NO_INPUT_USER_FACING,
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        internal_reason_code="tool_error_pending_reconciliation_no_input",
        blocked_tool=blocked_tool,
    )


# Streak threshold at which the copilot hard-aborts a tool call because the
# same action sequence has repeated run-over-run with no intervening success.
# The streak counter is incremented in ``update_repeated_failure_state`` AFTER
# each run, so the abort fires when the 4th consecutive run against the same
# action fingerprint enters ``_tool_loop_error`` (streak == 3 at entry, one
# per each of the three preceding identical runs). Calibration note: the
# repeated-frontier streak in failure_tracking.py uses STOP_AT=3 for the same
# shape of escalation.
REPEATED_ACTION_STREAK_ABORT_AT = 3
MAX_CHALLENGE_GATED_PROXY_RETRIES = 1

_STRUCTURED_BLOCKER_KEY_TERMS: frozenset[str] = frozenset(
    {
        "blocker",
        "blocked",
        "captcha",
        "challenge",
        "human_verification",
        "verification",
    }
)
_STRUCTURED_BLOCKER_MESSAGE_KEYS: frozenset[str] = frozenset(
    {
        "blocker_message",
        "blocked_message",
        "captcha_message",
        "challenge_message",
        "human_verification_message",
    }
)
_ANTI_BOT_BLOCKER_TERMS: tuple[str, ...] = (
    "access denied",
    "anti-bot",
    "bot block",
    "browser access barrier",
    "browser or environment port block",
    "browser port forbidden",
    "browser refused to render",
    "browser_port_forbidden",
    "browser_or_environment_port_block",
    "captcha",
    "challenge",
    "human verification",
    "port-forbidden",
    "requested port",
    "verify you are human",
)
# Multi-word anti-bot phrases only: the bare tokens ``captcha``/``challenge`` are
# excluded so business text mentioning them does not false-positive when a code-block
# value is scanned regardless of its key.
_BROAD_SINGLE_TOKEN_TERMS: frozenset[str] = frozenset({"captcha", "challenge"})
_ANTI_BOT_BLOCKER_PHRASES: tuple[str, ...] = tuple(
    term for term in _ANTI_BOT_BLOCKER_TERMS if term not in _BROAD_SINGLE_TOKEN_TERMS
)
# Strict subset of ``_STRUCTURED_BLOCKER_KEY_TERMS`` for the flag/status rules that
# scan arbitrary code-block JSON; broad terms like ``verification`` stay string-only.
_STRICT_BLOCKER_FLAG_TERMS: frozenset[str] = frozenset(
    {
        "blocker",
        "blocked",
        "captcha",
        "challenge",
        "human_verification",
    }
)
_BLOCKER_STATUS_KEYS: frozenset[str] = frozenset({"status", "state"})
_BLOCKER_SIBLING_MESSAGE_KEYS: frozenset[str] = frozenset({"reason", "message", "error", "failure_reason"})
_MAX_BLOCKER_STATUS_VALUE_LEN = 80


def _is_code_block_type(block_type: object) -> bool:
    return isinstance(block_type, str) and block_type.strip().upper() == BlockType.CODE.value.upper()


def _normalize_structured_key(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _looks_like_anti_bot_blocker(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in _ANTI_BOT_BLOCKER_TERMS)


def _looks_like_anti_bot_phrase(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _ANTI_BOT_BLOCKER_PHRASES)


def _structured_blocker_message(
    value: object,
    *,
    depth: int = 0,
    include_flag_keys: bool = False,
    key_terms: frozenset[str] = _STRUCTURED_BLOCKER_KEY_TERMS,
    declared_keys: frozenset[str] = frozenset(),
    scan_all_values_for_anti_bot: bool = False,
) -> str | None:
    if depth > 5:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = _normalize_structured_key(key)
            if normalized_key in declared_keys:
                continue
            if not isinstance(item, str) or not item.strip():
                continue
            has_blocker_key = normalized_key in _STRUCTURED_BLOCKER_MESSAGE_KEYS or any(
                term in normalized_key for term in key_terms
            )
            if (
                has_blocker_key
                or (
                    normalized_key in {"message", "error", "failure_reason", "reason"}
                    and _looks_like_anti_bot_blocker(item)
                )
                or (scan_all_values_for_anti_bot and _looks_like_anti_bot_phrase(item))
            ):
                return item.strip()[:240]
        for item in value.values():
            nested = _structured_blocker_message(
                item,
                depth=depth + 1,
                include_flag_keys=include_flag_keys,
                key_terms=key_terms,
                declared_keys=declared_keys,
                scan_all_values_for_anti_bot=scan_all_values_for_anti_bot,
            )
            if nested:
                return nested
        if include_flag_keys:
            flagged = _blocker_flag_or_status_message(value)
            if flagged:
                return flagged
    elif isinstance(value, list):
        for item in value:
            nested = _structured_blocker_message(
                item,
                depth=depth + 1,
                include_flag_keys=include_flag_keys,
                key_terms=key_terms,
                declared_keys=declared_keys,
                scan_all_values_for_anti_bot=scan_all_values_for_anti_bot,
            )
            if nested:
                return nested
    return None


def _blocker_flag_or_status_message(value: dict[str, Any]) -> str | None:
    for key, item in value.items():
        normalized_key = _normalize_structured_key(key)
        if item is True and any(term in normalized_key for term in _STRICT_BLOCKER_FLAG_TERMS):
            for sibling_key, sibling_item in value.items():
                if (
                    _normalize_structured_key(sibling_key) in _BLOCKER_SIBLING_MESSAGE_KEYS
                    and isinstance(sibling_item, str)
                    and sibling_item.strip()
                ):
                    return sibling_item.strip()[:240]
            return f"The run output flagged {normalized_key.replace('_', ' ')}."
        if (
            isinstance(item, str)
            and normalized_key in _BLOCKER_STATUS_KEYS
            and 0 < len(item.strip()) <= _MAX_BLOCKER_STATUS_VALUE_LEN
            and any(term in item.strip().lower() for term in _STRICT_BLOCKER_FLAG_TERMS)
        ):
            return f"The run output reported status '{item.strip()}'."
    return None


def _declared_code_output_keys(copilot_ctx: Any, block_label: object) -> frozenset[str]:
    """Output keys the block's code-artifact metadata declares as goal content
    (claimed-outcome ids, entities, required tokens) — the #12034 typed source.
    A declared key is never string-matched into a blocker signal."""
    metadata = getattr(copilot_ctx, "code_artifact_metadata", None) if copilot_ctx is not None else None
    if not isinstance(metadata, dict) or not isinstance(block_label, str):
        return frozenset()
    entry = metadata.get(block_label)
    if not isinstance(entry, dict):
        return frozenset()
    declared: set[str] = set()
    claims = entry.get("claimed_outcomes")
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, dict):
            continue
        for field_name in ("id", "entities", "required_tokens"):
            value = claim.get(field_name)
            values = value if isinstance(value, list) else [value]
            declared.update(
                _normalize_structured_key(item) for item in values if isinstance(item, str) and item.strip()
            )
    return frozenset(declared)


def _run_blocks_structured_blocker_message(result: dict[str, Any], copilot_ctx: Any = None) -> str | None:
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    direct = _structured_blocker_message({key: value for key, value in data.items() if key != "blocks"})
    if direct:
        return direct
    blocks = data.get("blocks")
    if not isinstance(blocks, list):
        return None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("block_type")
        if block.get("status") != "completed":
            continue
        if _is_code_block_type(block_type):
            # Code-block outputs are arbitrary JSON the model authored: key matching
            # uses the strict term set (broad terms like ``verification`` belong to
            # the page-text arms) and metadata-declared goal keys are exempt. A value
            # carrying a real anti-bot phrase is still caught regardless of its key.
            blocker = _structured_blocker_message(
                block.get("extracted_data"),
                include_flag_keys=True,
                key_terms=_STRICT_BLOCKER_FLAG_TERMS,
                declared_keys=_declared_code_output_keys(copilot_ctx, block.get("label")),
                scan_all_values_for_anti_bot=True,
            )
        elif block_type in _DATA_PRODUCING_BLOCK_TYPES:
            payload = _block_data_payload(block.get("extracted_data"), block_type)
            blocker = _structured_blocker_message(payload)
        else:
            continue
        if blocker:
            return blocker
    return None


def _artifact_challenge_flag_from_result(result: dict[str, Any], copilot_ctx: Any = None) -> str | None:
    """First typed anti-bot artifact marker in the run output, or ``None``. This is
    the artifact carrier; free-text scans are not. Only block outputs and registered
    output parameters are typed payloads, so their string marker values count; the
    run envelope's own string fields are prose/status (``failure_reason`` etc.) and
    are scanned for typed boolean flags only, never marker values."""
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    flag = artifact_challenge_flag_key(
        {key: value for key, value in data.items() if key != "blocks"},
        match_marker_values=False,
    )
    if flag:
        return flag
    blocks = data.get("blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict) or block.get("status") != "completed":
                continue
            declared_keys = (
                _declared_code_output_keys(copilot_ctx, block.get("label"))
                if _is_code_block_type(block.get("block_type"))
                else frozenset()
            )
            flag = artifact_challenge_flag_key(block.get("extracted_data"), declared_keys=declared_keys)
            if flag:
                return flag
    for registered in _registered_output_parameter_payloads(data):
        flag = artifact_challenge_flag_key(registered.get("value"))
        if flag:
            return flag
    return None


def _is_blocker_term_key(key: object, declared_keys: frozenset[str] = frozenset()) -> bool:
    normalized_key = _normalize_structured_key(key)
    if normalized_key in declared_keys:
        return False
    return any(term in normalized_key for term in _STRICT_BLOCKER_FLAG_TERMS)


def _code_output_contains_collection(
    value: Any, *, depth: int = 0, declared_keys: frozenset[str] = frozenset()
) -> bool:
    if depth > 5:
        return False
    if isinstance(value, (list, tuple)):
        return True
    if isinstance(value, dict):
        return any(
            _code_output_contains_collection(item, depth=depth + 1, declared_keys=declared_keys)
            for key, item in value.items()
            if not _is_blocker_term_key(key, declared_keys)
        )
    return False


def _code_output_has_goal_content(value: Any, *, depth: int = 0, declared_keys: frozenset[str] = frozenset()) -> bool:
    """Goal content in a code block's output: a non-empty string, truthy number, or
    non-empty collection surviving after blocker-term, status, and boolean entries
    are stripped (status/state values are machine shape, not goal data)."""
    if depth > 5:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    if isinstance(value, dict):
        return any(
            _code_output_has_goal_content(item, depth=depth + 1, declared_keys=declared_keys)
            for key, item in value.items()
            if not _is_blocker_term_key(key, declared_keys)
            and _normalize_structured_key(key) not in _BLOCKER_STATUS_KEYS
        )
    return False


def _metadata_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _metadata_goal_value_paths(value: Any) -> list[str]:
    # Keep in sync with workflow_update._artifact_goal_value_paths; duplicated
    # locally to avoid importing the authoring validator into runtime blockers.
    return [path for path in _metadata_string_list(value) if not path.casefold().startswith("<fill")]


def _goal_value_paths_for_code_block(copilot_ctx: Any | None, label: Any) -> list[str]:
    if copilot_ctx is None or not isinstance(label, str):
        return []
    metadata = getattr(copilot_ctx, "code_artifact_metadata", None)
    if not isinstance(metadata, dict):
        return []
    entry = metadata.get(label)
    if not isinstance(entry, dict):
        return []

    paths: list[str] = []
    seen: set[str] = set()
    for row_group in (entry.get("claimed_outcomes"), entry.get("terminal_verifier_expectations")):
        rows = [row for row in row_group if isinstance(row, dict)] if isinstance(row_group, list) else []
        for row in rows:
            for path in _metadata_goal_value_paths(row.get("goal_value_paths")):
                if path not in seen:
                    seen.add(path)
                    paths.append(path)
    return paths


def _parse_metadata_extraction_schema(value: Any) -> dict[str, Any] | None:
    # Keep in sync with workflow_update._parse_extraction_schema; duplicated locally
    # so runtime blockers do not import the authoring validator.
    if isinstance(value, dict):
        return value or None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.casefold() in {"null", "none"} or text.casefold().startswith("<fill"):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) and parsed else None


def _extraction_schema_for_code_block(copilot_ctx: Any | None, label: Any) -> dict[str, Any] | None:
    if copilot_ctx is None or not isinstance(label, str):
        return None
    metadata = getattr(copilot_ctx, "code_artifact_metadata", None)
    if not isinstance(metadata, dict):
        return None
    entry = metadata.get(label)
    if not isinstance(entry, dict):
        return None
    # claimed_outcomes wins when both groups declare a schema; they are expected to carry the same value.
    for row_group in (entry.get("claimed_outcomes"), entry.get("terminal_verifier_expectations")):
        rows = [row for row in row_group if isinstance(row, dict)] if isinstance(row_group, list) else []
        for row in rows:
            schema = _parse_metadata_extraction_schema(row.get("extraction_schema"))
            if schema is not None:
                return schema
    return None


_GOAL_PATH_INDEX_PATTERN = re.compile(r"\[\d+\]")


def _normalize_goal_value_path(path: str) -> list[str]:
    normalized = path.strip()
    if normalized.startswith("$."):
        normalized = normalized[2:]
    elif normalized.startswith("$"):
        normalized = normalized[1:]
    normalized = normalized.replace("[*]", "[]")
    normalized = _GOAL_PATH_INDEX_PATTERN.sub("[]", normalized)
    return [part for part in normalized.split(".") if part]


def _iter_goal_value_path_values(value: Any, path_parts: list[str]) -> list[Any]:
    if not path_parts:
        return [value]
    current_part = path_parts[0]
    if isinstance(value, (list, tuple, set)):
        child_parts = path_parts[1:] if current_part == "[]" else path_parts
        expanded_values: list[Any] = []
        for item in value:
            expanded_values.extend(_iter_goal_value_path_values(item, child_parts))
        return expanded_values

    if current_part == "[]":
        return []

    expand_collection = current_part.endswith("[]")
    key = current_part[:-2] if expand_collection else current_part
    if not isinstance(value, Mapping) or key not in value:
        return []

    next_value = value.get(key)
    remaining = path_parts[1:]
    if expand_collection:
        if isinstance(next_value, (list, tuple)):
            child_values: list[Any] = []
            for item in next_value:
                child_values.extend(_iter_goal_value_path_values(item, remaining))
            return child_values
        return []
    return _iter_goal_value_path_values(next_value, remaining)


def _code_output_goal_paths_have_content(value: Any, goal_value_paths: list[str]) -> bool:
    for path in goal_value_paths:
        path_parts = _normalize_goal_value_path(path)
        values = _iter_goal_value_path_values(value, path_parts)
        if not values and _goal_value_path_targets_registered_download(path):
            values = _registered_download_output_values(value)
        if not any(_code_output_goal_path_value_has_content(item) for item in values):
            return False
    return True


def _code_output_goal_path_value_has_content(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    return _code_output_has_goal_content(value)


def _goal_value_path_targets_registered_download(path: str) -> bool:
    normalized = path.strip()
    if normalized.startswith("$."):
        normalized = normalized[2:]
    elif normalized.startswith("$"):
        normalized = normalized[1:]
    head = normalized.split(".", 1)[0].split("[", 1)[0].strip()
    return head in REGISTERED_DOWNLOAD_OUTPUT_KEYS


def _registered_download_output_values(value: Any) -> list[Any]:
    if not isinstance(value, Mapping):
        return []
    return [value[key] for key in REGISTERED_DOWNLOAD_OUTPUT_KEYS if key in value]


def _code_output_has_registered_download_content(value: Any) -> bool:
    return any(_code_output_has_goal_content(item) for item in _registered_download_output_values(value))


def _active_block_run_budget_seconds(ctx: AgentContext) -> int:
    remaining = _copilot_seconds_remaining(ctx)
    if remaining is None:
        return PER_TOOL_CALL_BUDGET_SECONDS
    remaining_after_reply_reserve = remaining - COPILOT_FINAL_REPLY_RESERVE_SECONDS
    return max(1, min(PER_TOOL_CALL_BUDGET_SECONDS, int(remaining_after_reply_reserve)))


def _late_block_running_call_signal(ctx: AgentContext, tool_name: str) -> CopilotToolBlockerSignal | None:
    remaining = _copilot_seconds_remaining(ctx)
    if remaining is None or remaining > COPILOT_FINAL_REPLY_RESERVE_SECONDS:
        return None

    last_failed_workflow_yaml = getattr(ctx, "last_failed_workflow_yaml", None)
    last_good_workflow_yaml = getattr(ctx, "last_good_workflow_yaml", None)
    if (
        isinstance(last_failed_workflow_yaml, str)
        and last_failed_workflow_yaml
        and isinstance(last_good_workflow_yaml, str)
        and last_good_workflow_yaml
    ):
        agent_steering = (
            f"Wall-clock budget too low to retry: about {int(max(0.0, remaining))}s remain of the "
            f"{TOTAL_TIMEOUT_SECONDS}s session budget. A verified workflow exists from before the failure. "
            "Do NOT call update_and_run_blocks or run_blocks_and_collect_debug again. REPLY now: summarize "
            "what worked, name the block that failed, and tell the user they can keep the verified prefix or discard."
        )
    elif isinstance(last_failed_workflow_yaml, str) and last_failed_workflow_yaml:
        agent_steering = (
            f"Less than {COPILOT_FINAL_REPLY_RESERVE_SECONDS} seconds remain in this Copilot turn "
            "after the previous workflow run failed. Do NOT retry block-running tools. Use only existing "
            "run evidence and quick browser inspection tools such as get_run_results, evaluate, or "
            "get_browser_screenshot if one more read is needed. If the current page contains the requested "
            "answer, answer from that observed page evidence. If evidence is incomplete, report exactly "
            "which browser state was verified and which requested data remains unverified. Never repeat "
            "this tool-error text as the user-facing answer."
        )
    else:
        agent_steering = (
            f"Less than {COPILOT_FINAL_REPLY_RESERVE_SECONDS} seconds remain in this Copilot turn. "
            "Do NOT start another block-running tool call; reply to the user with the workflow draft and "
            "progress gathered so far, and make clear which parts have not been verified end-to-end."
        )

    user_facing = "I'm running out of time on this turn. I'll wrap up with what I have so far."
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=agent_steering,
        user_facing_reason=user_facing,
        recovery_hint="stop",
        cleared_by_tools=frozenset(),
        # The "wrap up with what we have" semantic means the draft saved
        # earlier in the turn should still surface — only the chat reply
        # is overridden by the renderer.
        preserves_workflow_draft=True,
        renders_final_reply=False,
        internal_reason_code="tool_error_late_block_running",
        blocked_tool=tool_name,
    )


def _allows_post_run_current_page_inspection_budget_bypass(ctx: AgentContext, *, use_current_page: bool) -> bool:
    if not use_current_page:
        return False
    run_id = getattr(ctx, "last_run_blocks_workflow_run_id", None)
    if not isinstance(run_id, str) or not run_id:
        return False
    if getattr(ctx, "last_test_ok", None) is None:
        return False
    return getattr(ctx, "post_run_current_page_inspection_workflow_run_id", None) != run_id


def _post_budget_page_inspection_signal(ctx: AgentContext, tool_name: str) -> CopilotToolBlockerSignal | None:
    if getattr(ctx, "post_budget_page_inspection_required", False) is not True:
        return None

    url = getattr(ctx, "post_budget_page_inspection_url", None)
    run_id = getattr(ctx, "post_budget_page_inspection_run_id", None)
    url_text = f" at {url}" if isinstance(url, str) and url else ""
    run_text = f" for run {run_id}" if isinstance(run_id, str) and run_id else ""
    agent_steering = (
        f"The prior PER_TOOL_BUDGET run{run_text} advanced the live browser{url_text}. "
        "Before another block-running tool, inspect the current browser page with "
        'inspect_page_for_composition(target_url="current_page"). Generic screenshot/evaluate reads can '
        "help answer the user, but they do not satisfy the bounded page-evidence contract for workflow "
        "mutations. If the observed page evidence already contains the requested result or a no-results "
        "state, answer from that evidence instead of rerunning the search. If evidence shows a missing "
        "page-state change, then run only the smaller missing block after that inspection."
    )
    user_facing = "I need to inspect the current page state before running more steps."
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=agent_steering,
        user_facing_reason=user_facing,
        recovery_hint="retry_with_different_tool",
        cleared_by_tools=PAGE_SCHEMA_CONTEXT_TOOLS,
        preserves_workflow_draft=True,
        renders_final_reply=False,
        internal_reason_code="tool_error_post_budget_page_inspection_required",
        blocked_tool=tool_name,
    )


def _proxy_value_signature(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _anti_bot_retry_changes_proxy(ctx: AgentContext, arguments: dict[str, Any] | None) -> bool:
    if not isinstance(arguments, dict):
        return False
    workflow_yaml = arguments.get("workflow_yaml")
    if not isinstance(workflow_yaml, str) or not workflow_yaml.strip():
        return False

    prior_yaml = getattr(ctx, "last_failed_workflow_yaml", None) or getattr(ctx, "workflow_yaml", None)
    if not isinstance(prior_yaml, str) or not prior_yaml.strip():
        return False

    new_proxy_present, new_proxy = _raw_yaml_proxy_location(workflow_yaml)
    if not new_proxy_present:
        return False
    old_proxy_present, old_proxy = _raw_yaml_proxy_location(prior_yaml)
    return (not old_proxy_present) or _proxy_value_signature(new_proxy) != _proxy_value_signature(old_proxy)


def _challenge_gated_proxy_retry_allowed(ctx: AgentContext, arguments: dict[str, Any] | None) -> bool:
    if not _anti_bot_retry_changes_proxy(ctx, arguments):
        return False
    retry_count = getattr(ctx, "challenge_gated_proxy_retry_count", 0)
    if not isinstance(retry_count, int):
        retry_count = 0
    if retry_count >= MAX_CHALLENGE_GATED_PROXY_RETRIES:
        return False
    ctx.challenge_gated_proxy_retry_count = retry_count + 1
    return True


def _last_run_has_terminal_anti_bot_blocker(ctx: AgentContext) -> bool:
    anti_bot_reason = getattr(ctx, "last_test_anti_bot", None)
    if not isinstance(anti_bot_reason, str) or not anti_bot_reason.strip():
        return False
    failure_reason = getattr(ctx, "last_test_failure_reason", None)
    if not isinstance(failure_reason, str) or not failure_reason.strip():
        return False
    lowered = failure_reason.lower()
    if "blocker" in lowered and _looks_like_anti_bot_blocker(lowered):
        return True

    evidence = getattr(ctx, "composition_page_evidence", None)
    challenge_state = evidence.get("challenge_state") if isinstance(evidence, dict) else None
    challenge_gates_submit = isinstance(challenge_state, dict) and challenge_state.get("gates_submit_controls") is True
    if not (challenge_gates_submit or "challenge-gated disabled submit/search control" in anti_bot_reason):
        return False

    return "disabled" in lowered and any(
        term in lowered for term in ("submit", "search", "button", "control", "element")
    )


def _challenge_gated_anti_bot_rerun_signal(
    ctx: AgentContext,
    arguments: dict[str, Any] | None,
    tool_name: str,
) -> CopilotToolBlockerSignal | None:
    if not _last_run_has_terminal_anti_bot_blocker(ctx):
        return None
    if tool_name == "update_and_run_blocks" and _challenge_gated_proxy_retry_allowed(ctx, arguments):
        return None

    failure_reason = getattr(ctx, "last_test_failure_reason", "")
    agent_steering = (
        "The prior run confirmed an anti-bot challenge or blocker on the submit/search path, "
        f"and the latest failure_reason was: {str(failure_reason)[:240]}. Do NOT call "
        f"{tool_name} again with the same workflow/browser path. REPLY now with a blocker "
        "explanation that names the observed challenge/blocker or disabled submit/search control "
        "and describes what was tried. "
        "Ask whether to try a materially different proxy/location, entrypoint, or alternate source."
    )
    user_facing = (
        "The site's verification challenge is still keeping the submit/search control disabled, so I stopped "
        "instead of retrying the same workflow path."
    )
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=agent_steering,
        user_facing_reason=user_facing,
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code="tool_error_challenge_gated_submit_disabled",
        blocked_tool=tool_name,
    )


def _tool_loop_error(ctx: AgentContext, tool_name: str, arguments: dict[str, Any] | None = None) -> str | None:
    refresh_held_loop_blocker_evidence(ctx)
    if tool_name in _CURRENT_PAGE_TERMINAL_CHALLENGE_TOOLS:
        current_page_challenge_signal = _current_page_terminal_challenge_signal(ctx, arguments, tool_name)
        if current_page_challenge_signal is not None:
            return _emit_tool_blocker_signal(ctx, current_page_challenge_signal)

    uncovered_output_steer = uncovered_output_reject_scout_steer_signal(ctx, tool_name)
    if uncovered_output_steer is not None:
        return _emit_tool_blocker_signal(ctx, uncovered_output_steer)

    persistence_signal = synthesized_block_persistence_signal(ctx, tool_name)
    if persistence_signal is not None:
        grounding_signal = _recorded_outcome_grounding_signal(ctx, tool_name)
        if grounding_signal is not None:
            LOG.info(
                "copilot recorded outcome grounding enforced over persistence force",
                tool_name=tool_name,
            )
            return _emit_tool_blocker_signal(ctx, grounding_signal)
        return _emit_tool_blocker_signal(ctx, persistence_signal)

    # While a typed output-contract actuation ladder is still unresolved for the authoring tools, the
    # bounded ladder (not a generic loop backstop) owns the turn's outcome; the guards re-engage the moment
    # the ladder reaches an advisory-consumed run or a typed terminal.
    output_contract_owns_turn = (
        tool_name in _OUTPUT_CONTRACT_LADDER_AUTHORING_TOOLS and output_contract_ladder_unresolved(ctx)
    )

    if not output_contract_owns_turn:
        detected = detect_failed_tool_step_loop_for_ctx(ctx, tool_name, arguments or {})
        if detected is not None:
            return _emit_tool_blocker_signal(
                ctx,
                _build_loop_blocker_signal(detected, tool_name=tool_name, evidence=loop_blocker_evidence_from_ctx(ctx)),
            )

    # Consecutive same-name guard: false-positives on the intended iterative
    # build (one new block per update_and_run_blocks). Block-running tools
    # rely on the progress-aware checks below instead. fill_credential_field is
    # exempt because a username+password+TOTP form legitimately needs three
    # consecutive calls; its failed-step guard above stays argument-aware.
    tracker = getattr(ctx, "consecutive_tool_tracker", None)
    if (
        isinstance(tracker, list)
        and tool_name not in _CONSECUTIVE_LOOP_GUARD_EXEMPT_TOOLS
        and not output_contract_owns_turn
    ):
        detected = detect_tool_loop(tracker, tool_name, arguments)
        if detected is not None:
            return _emit_tool_blocker_signal(
                ctx,
                _build_loop_blocker_signal(detected, tool_name=tool_name, evidence=loop_blocker_evidence_from_ctx(ctx)),
            )

    if tool_name == "update_workflow" or tool_name in BLOCK_RUNNING_TOOLS:
        active_terminal_signal = _active_run_terminal_evidence_signal(ctx, tool_name)
        if active_terminal_signal is not None:
            return _emit_tool_blocker_signal(ctx, active_terminal_signal)

    # Hard-abort when the agent has re-fired the same action sequence against
    # the page N times without intervening success. This is the signal that
    # the form is blocked (captcha / anti-bot / error banner the agent isn't
    # detecting) and further attempts will just burn the tool timeout. Scoped
    # to block-running tools so planning/metadata tools (update_workflow,
    # list_credentials, get_run_results) stay unaffected.
    if tool_name in BLOCK_RUNNING_TOOLS:
        # Reconciliation guard: the previous block-running tool call exited
        # without a trustworthy terminal status for its workflow run (the
        # watchdog's stagnation / ceiling / task_exit_unfinalized paths, or
        # the SKY-9167 post-drain branch where the row read as ``canceled``,
        # non-final, or unreadable). Block further block-running calls until
        # ``get_run_results`` clears the flag — prevents the LLM from
        # auto-retrying a mutation block whose side effects may already
        # have landed.
        pending_run_id = getattr(ctx, "pending_reconciliation_run_id", None)
        if isinstance(pending_run_id, str) and pending_run_id:
            if getattr(ctx, "pending_reconciliation_requires_user_input", False) is True:
                return _emit_tool_blocker_signal(
                    ctx,
                    _pending_reconciliation_requires_input_signal(
                        pending_run_id=pending_run_id, blocked_tool=tool_name
                    ),
                )
            return _emit_tool_blocker_signal(
                ctx,
                _pending_reconciliation_no_input_signal(pending_run_id=pending_run_id, blocked_tool=tool_name),
            )

        inspection_signal = _post_budget_page_inspection_signal(ctx, tool_name)
        if inspection_signal is not None:
            return _emit_tool_blocker_signal(ctx, inspection_signal)

        # Terminal anti-bot evidence should produce the final user-facing reply
        # before the generic budget rerun path can ask for another attempt.
        post_budget_challenge_signal = _post_budget_terminal_challenge_signal(ctx, arguments, tool_name)
        if post_budget_challenge_signal is not None:
            return _emit_tool_blocker_signal(ctx, post_budget_challenge_signal)

        budget_signal = _per_tool_budget_problem_rerun_signal(ctx, arguments, tool_name)
        if budget_signal is not None:
            return _emit_tool_blocker_signal(ctx, budget_signal)

        upstream_replay_signal = _post_budget_upstream_replay_signal(ctx, arguments, tool_name)
        if upstream_replay_signal is not None:
            return _emit_tool_blocker_signal(ctx, upstream_replay_signal)

        challenge_signal = _challenge_gated_anti_bot_rerun_signal(ctx, arguments, tool_name)
        if challenge_signal is not None:
            return _emit_tool_blocker_signal(ctx, challenge_signal)

        streak_raw = getattr(ctx, "repeated_action_fingerprint_streak_count", 0)
        streak = streak_raw if isinstance(streak_raw, int) else 0
        if streak >= REPEATED_ACTION_STREAK_ABORT_AT:
            agent_steering = (
                f"Repeated-action abort: the last {streak} runs fired the same "
                "action sequence against the page without making progress. "
                "The site is likely blocked by a captcha, popup, anti-bot "
                "challenge, or hidden validation error that the agent is not "
                "detecting. Do NOT retry this tool — conclude the workflow is "
                "not automatable as-is and report back to the user."
            )
            user_facing = (
                "I tried the same actions a few times without making progress. The site looks blocked. I'll stop here."
            )
            return _emit_tool_blocker_signal(
                ctx,
                CopilotToolBlockerSignal(
                    blocker_kind="tool_error",
                    agent_steering_text=agent_steering,
                    user_facing_reason=user_facing,
                    recovery_hint="stop",
                    cleared_by_tools=frozenset(),
                    internal_reason_code="tool_error_repeated_action_abort",
                    blocked_tool=tool_name,
                ),
            )

        # Within-turn fail-fast for permanent navigation errors (DNS / cert /
        # SSL / invalid URL). The enforcement-loop stop nudge only runs
        # BETWEEN agent turns, so without this check the LLM is free to make
        # speculative within-turn retries (e.g. drop the `www` subdomain and
        # try again) before the nudge fires. update_and_run_blocks internally
        # calls _update_workflow which clears the flag, so this check must
        # run before that — hence at the tool entrypoint, not inside the run
        # body.
        prior_nav_error = getattr(ctx, "last_test_non_retriable_nav_error", None)
        if isinstance(prior_nav_error, str) and prior_nav_error:
            agent_steering = (
                f"Prior run in this turn hit a permanent navigation error "
                f"({prior_nav_error[:200]}). Do NOT retry — the URL is unreachable "
                "regardless of subdomain or path variations. Reply to the user "
                "explaining the failure and asking them to verify the URL."
            )
            user_facing = "The URL I tried isn't reachable. Tell me the correct address and I'll try again."
            return _emit_tool_blocker_signal(
                ctx,
                CopilotToolBlockerSignal(
                    blocker_kind="tool_error",
                    agent_steering_text=agent_steering,
                    user_facing_reason=user_facing,
                    recovery_hint="ask_user_clarifying",
                    cleared_by_tools=frozenset(),
                    internal_reason_code="tool_error_non_retriable_nav",
                    blocked_tool=tool_name,
                ),
            )

        late_signal = _late_block_running_call_signal(ctx, tool_name)
        if late_signal is not None:
            return _emit_tool_blocker_signal(ctx, late_signal)
    grounding_signal = _recorded_outcome_grounding_signal(ctx, tool_name)
    if grounding_signal is not None:
        return _emit_tool_blocker_signal(ctx, grounding_signal)
    return None


_build_loop_blocker_signal = build_loop_blocker_signal


def _recorded_outcome_grounding_signal(ctx: AgentContext, tool_name: str) -> CopilotToolBlockerSignal | None:
    if tool_name not in _RECORDED_OUTCOME_GROUNDING_MUTATION_TOOLS:
        return None
    if maybe_satisfy_recorded_outcome_grounding_requirement(ctx):
        return None
    if not recorded_outcome_grounding_requires_current_page(ctx):
        return None
    requirement = ctx.recorded_outcome_grounding_requirement
    if requirement is None:
        return None
    payload: AuthorTimeGateAblationPayload = {
        "phase": requirement.phase,
        "outcome_reason_code": requirement.reason_code,
        "workflow_run_id": requirement.workflow_run_id,
        "block_labels": list(requirement.block_labels),
        "required_tool": requirement.required_tool,
        "required_target_url": requirement.required_target_url,
    }
    if record_author_time_gate_ablation_event(
        ctx,
        gate_id="recorded_outcome_grounding",
        reason_code="recorded_outcome_grounding_required",
        fingerprint=requirement.structural_key,
        blocked_tool=tool_name,
        payload=payload,
    ):
        return None
    return CopilotToolBlockerSignal(
        blocker_kind="missing_required_context",
        agent_steering_text=(
            "The repeated recorded build-test outcome is still ungrounded. Call "
            'inspect_page_for_composition(target_url="current_page") and use that bounded page '
            "evidence before the next workflow mutation or browser mutation."
        ),
        user_facing_reason="I need to inspect the current page before changing the workflow again.",
        recovery_hint="retry_with_different_tool",
        cleared_by_tools=frozenset({"inspect_page_for_composition"}),
        renders_final_reply=False,
        internal_reason_code="recorded_outcome_grounding_required",
        blocked_tool=tool_name,
    )


def _analyze_run_blocks(
    result: dict[str, Any], copilot_ctx: Any | None = None
) -> tuple[str | None, bool, list[dict] | None]:
    """Single-pass analysis of run result blocks.

    Returns ``(anti_bot_match, has_empty_data_blocks, failure_categories)``
    by iterating the block list once. Classification delegates to
    :func:`~skyvern.forge.failure_classifier.classify_from_failure_reason`.
    When ``data["failure_categories"]`` is already populated (pre-run
    short-circuit with no blocks), honor it instead of re-classifying.
    """
    data = result.get("data")
    if not isinstance(data, dict):
        return None, False, None

    anti_bot_match: str | None = None

    precomputed_categories = data.get("failure_categories")
    if isinstance(precomputed_categories, list) and precomputed_categories:
        for cat in precomputed_categories:
            if isinstance(cat, dict) and trusted_terminal_challenge_category_name(cat) is not None:
                anti_bot_match = cat.get("reasoning", "anti-bot pattern detected")
                break
        return anti_bot_match, False, precomputed_categories

    # Collect texts for scanning and data-block stats in one pass
    texts_to_scan: list[str] = []
    error = result.get("error")
    if isinstance(error, str):
        texts_to_scan.append(error)
    html = data.get("visible_elements_html")
    if isinstance(html, str):
        texts_to_scan.append(html)
    failure_reason = data.get("failure_reason")
    if isinstance(failure_reason, str):
        texts_to_scan.append(failure_reason)
    page_title = data.get("page_title")
    if isinstance(page_title, str):
        texts_to_scan.append(page_title)
    action_trace_summary = data.get("action_trace_summary")
    if isinstance(action_trace_summary, list):
        texts_to_scan.extend(str(item) for item in action_trace_summary if isinstance(item, str))

    has_data_blocks = False
    any_data_output = False
    missing_metadata_goal_content = False
    complete_structured_record_output = False

    blocks = data.get("blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            reason = block.get("failure_reason")
            if isinstance(reason, str):
                texts_to_scan.append(reason)
            block_type = block.get("block_type")
            if block.get("status") != "completed":
                continue
            if block_type in _DATA_PRODUCING_BLOCK_TYPES:
                has_data_blocks = True
                payload = _block_data_payload(block.get("extracted_data"), block_type)
                structured_blocker = _structured_blocker_message(payload)
                if structured_blocker:
                    texts_to_scan.append(structured_blocker)
                if _is_meaningful_extracted_data(payload):
                    any_data_output = True
            elif _is_code_block_type(block_type):
                extracted = block.get("extracted_data")
                if extracted is None:
                    continue
                declared_keys = _declared_code_output_keys(copilot_ctx, block.get("label"))
                structured_blocker = _structured_blocker_message(
                    extracted,
                    include_flag_keys=True,
                    key_terms=_STRICT_BLOCKER_FLAG_TERMS,
                    declared_keys=declared_keys,
                    scan_all_values_for_anti_bot=True,
                )
                if structured_blocker:
                    texts_to_scan.append(structured_blocker)
                output_parameter_payloads = _workflow_output_parameter_payloads(extracted)
                if output_parameter_payloads:
                    has_data_blocks = True
                    if any(_is_meaningful_extracted_data(value) for value in output_parameter_payloads.values()):
                        any_data_output = True
                    if any(_structured_record_has_goal_content(value) for value in output_parameter_payloads.values()):
                        complete_structured_record_output = True
                extraction_schema = _extraction_schema_for_code_block(copilot_ctx, block.get("label"))
                # Array-typed schemas would coerce the keyed dict return to [] (fill_missing_fields), making the
                # goal-path check below read a real extraction as empty; the keyed-return floor guarantees a dict.
                if (
                    extraction_schema is not None
                    and isinstance(extracted, dict)
                    and extraction_schema.get("type") != "array"
                ):
                    extracted = validate_and_fill_extraction_result(extracted, extraction_schema)
                goal_value_paths = _goal_value_paths_for_code_block(copilot_ctx, block.get("label"))
                if goal_value_paths:
                    has_data_blocks = True
                    if (
                        _code_output_has_registered_download_content(extracted)
                        or _code_output_goal_paths_have_content(extracted, goal_value_paths)
                        or _structured_record_has_goal_content(extracted)
                    ):
                        any_data_output = True
                    else:
                        # Terminal goal paths are conjunctive: one missing
                        # declared field means the block did not prove the
                        # requested outcome, even if another path had data.
                        missing_metadata_goal_content = True
                    # Goal-path contracts supersede the generic collection-shape
                    # fallback below; they are the stronger outcome evidence check.
                    continue
                # A code output joins the emptiness denominator only when it declares a
                # collection shape; action-only outputs are exempt.
                if _code_output_contains_collection(extracted, declared_keys=declared_keys):
                    has_data_blocks = True
                if _code_output_has_goal_content(extracted, declared_keys=declared_keys):
                    any_data_output = True

    top_level_output_payloads = _workflow_output_parameter_payloads(data.get("output"))
    if top_level_output_payloads:
        has_data_blocks = True
        if any(_is_meaningful_extracted_data(value) for value in top_level_output_payloads.values()):
            any_data_output = True
        if any(_structured_record_has_goal_content(value) for value in top_level_output_payloads.values()):
            complete_structured_record_output = True

    registered_payloads = _registered_output_parameter_payloads(data)
    if registered_payloads:
        has_data_blocks = True
        for registered in registered_payloads:
            value = registered.get("value")
            if _is_meaningful_extracted_data(value):
                any_data_output = True
            if _structured_record_has_goal_content(value):
                complete_structured_record_output = True
            structured_blocker = _structured_blocker_message(
                value,
                include_flag_keys=True,
                key_terms=_STRICT_BLOCKER_FLAG_TERMS,
                scan_all_values_for_anti_bot=True,
            )
            if structured_blocker:
                texts_to_scan.append(structured_blocker)

    combined = "\n".join(texts_to_scan)
    categories = classify_from_failure_reason(combined)
    if categories:
        for cat in categories:
            if cat.get("category") == "ANTI_BOT_DETECTION":
                if is_carrier_backed_category_entry(cat):
                    anti_bot_match = cat.get("reasoning", "anti-bot pattern detected")
                else:
                    LOG.info(
                        "copilot anti-bot classifier match keyword-only-suppressed",
                        workflow_run_id=data.get("workflow_run_id"),
                    )
                break

    if complete_structured_record_output:
        if missing_metadata_goal_content:
            LOG.info(
                "copilot run evidence: a complete structured-record output suppressed a "
                "per-block missing-metadata-goal-content signal",
                workflow_run_id=data.get("workflow_run_id"),
            )
        missing_metadata_goal_content = False
    empty_data_blocks = (has_data_blocks and not any_data_output) or missing_metadata_goal_content
    return anti_bot_match, empty_data_blocks, categories


def _structured_record_has_goal_content(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    candidates = [value]
    candidates.extend(
        nested
        for key, nested in value.items()
        if isinstance(key, str) and key.endswith("_output") and isinstance(nested, dict)
    )
    return any(_structured_record_candidate_has_goal_content(candidate) for candidate in candidates)

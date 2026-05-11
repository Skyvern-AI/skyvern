"""Copilot agent tools — native handlers, hooks, and registration."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlparse

import structlog
import yaml
from agents import ToolGuardrailFunctionOutput, ToolInputGuardrail, ToolInputGuardrailData, function_tool
from agents.run_context import RunContextWrapper
from jinja2.sandbox import SandboxedEnvironment
from pydantic import ValidationError

from skyvern.forge import app
from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot.attribution import resolve_copilot_created_by_stamp
from skyvern.forge.sdk.copilot.block_goal_wrapping import wrap_block_goals
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    POST_INTERMEDIATE_SUCCESS_NUDGE,
    PROBABLE_SITE_BLOCK_STREAK_STOP_AT,
    TOTAL_TIMEOUT_SECONDS,
    _goal_likely_needs_more_blocks,
)
from skyvern.forge.sdk.copilot.failure_tracking import (
    PER_TOOL_BUDGET_FAILURE_CATEGORY,
    _canonical_block_config,
    compute_action_sequence_fingerprint,
    update_repeated_failure_state,
)
from skyvern.forge.sdk.copilot.loop_detection import (
    clear_failed_step_tracker_for_tools_in_ctx,
    detect_failed_tool_step_loop_for_ctx,
    detect_tool_loop,
    record_tool_step_result_for_ctx,
)
from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay
from skyvern.forge.sdk.copilot.narration import NarratorState
from skyvern.forge.sdk.copilot.narration import handler_available as narration_handler_available
from skyvern.forge.sdk.copilot.narration import narrator_poll_tick
from skyvern.forge.sdk.copilot.output_policy import (
    evaluate_output_policy,
    format_output_policy_tool_error,
    output_policy_verdict_to_trace_data,
)
from skyvern.forge.sdk.copilot.output_utils import (
    _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
    build_run_blocks_response,
    iter_failure_reasons,
    sanitize_tool_result_for_llm,
    truncate_output,
)
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext, ensure_browser_session
from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.routes.workflow_copilot import _process_workflow_yaml
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException
from skyvern.forge.sdk.workflow.models.parameter import (
    RESERVED_PARAMETER_KEYS,
    OutputParameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun, WorkflowRunStatus
from skyvern.schemas.proxy_location import ProxyLocation
from skyvern.schemas.workflows import BlockType
from skyvern.utils.yaml_loader import safe_load_no_dates
from skyvern.webeye.navigation import is_skip_inner_retry_error
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()

_FAILED_BLOCK_STATUSES: frozenset[str] = frozenset(
    {
        WorkflowRunStatus.failed.value,
        WorkflowRunStatus.terminated.value,
        WorkflowRunStatus.canceled.value,
        WorkflowRunStatus.timed_out.value,
    }
)
_DATA_PRODUCING_BLOCK_TYPES = frozenset({"EXTRACTION", "TEXT_PROMPT"})

# Block types whose ``block.output`` is a ``TaskOutput.from_task()`` envelope
# (schemas/tasks.py:TaskOutput) rather than the raw payload. The
# meaningful-data check must unwrap these via ``_block_data_payload`` before
# judging output, because envelope fields (task_id, status, artifact IDs) are
# always populated on a completed run and would otherwise mask empty
# extractions. This is a subset of ``_DATA_PRODUCING_BLOCK_TYPES`` — keep the
# two in sync when adding a new task-backed type. ``TEXT_PROMPT`` is
# deliberately excluded: its block.output is the raw LLM response dict (see
# ``TextPromptBlock.execute``), no envelope to strip.
_TASK_ENVELOPE_BLOCK_TYPES = frozenset({"EXTRACTION"})
assert _TASK_ENVELOPE_BLOCK_TYPES <= _DATA_PRODUCING_BLOCK_TYPES, (
    "_TASK_ENVELOPE_BLOCK_TYPES must be a subset of _DATA_PRODUCING_BLOCK_TYPES"
)

# Absolute upper bound on a single ``run_blocks`` tool invocation. Exists only
# as a last-resort trip wire for runaway loops — progressing runs should never
# approach this. The OpenAI Agents SDK wraps the tool in
# ``asyncio.wait_for(..., timeout=RUN_BLOCKS_SAFETY_CEILING_SECONDS)``; the
# inner poll loop leaves a 10 s headroom below this ceiling for orderly
# cleanup before the SDK cancels.
RUN_BLOCKS_SAFETY_CEILING_SECONDS = 1200  # 20 min

# Per-tool-call budget for active block runs — caps a single tool invocation
# below the session-level wall clock (``enforcement.TOTAL_TIMEOUT_SECONDS``,
# 600 s) so a long chain cannot consume the whole budget without giving the
# copilot a chance to issue a smaller chain. Quiet-block runs keep the longer
# ``RUN_BLOCKS_SAFETY_CEILING_SECONDS`` above.
PER_TOOL_CALL_BUDGET_SECONDS = 240

# Primary exit condition: seconds of no observed progress across the combined
# run / block / step heartbeat. Sized to accommodate the slowest single LLM
# round-trip (~30-60 s in practice) with headroom; going tighter risks
# false-positives on healthy runs.
RUN_BLOCKS_STAGNATION_WINDOW_SECONDS = 90

# Reserve final-reply room; active block runs shrink their own budget near the deadline.
COPILOT_FINAL_REPLY_RESERVE_SECONDS = 90

# 5 s balances responsiveness (18 samples inside the stagnation window) against
# DB load (240 polls worst case at the safety ceiling).
RUN_BLOCKS_POLL_INTERVAL_SECONDS = 5.0

# Detached cleanup tasks held here so the garbage collector does not drop them
# while they still have work to do, and so the "task exception was never
# retrieved" warning cannot fire — each task adds a done-callback that logs
# exceptions and removes itself from this set.
_DETACHED_CLEANUP_TASKS: set[asyncio.Task] = set()
_CREDENTIAL_ID_RE = re.compile(r"\bcred_[A-Za-z0-9][A-Za-z0-9_-]*\b")


async def _cancel_run_task_if_not_final(
    run_task: asyncio.Task,
    workflow_run_id: str,
) -> None:
    """Cancel ``run_task`` and reconcile the workflow run row to a terminal
    state.

    ``run_task.cancel()`` is synchronous — it just flips the cancel flag. We
    then wait briefly for ``execute_workflow``'s outer ``finally`` to drain
    its shielded ``_finalize_workflow_run_status`` call, which restores the
    real terminal status (``failed``/``terminated``/``timed_out``) even when
    we cancel mid-flight. After that we issue a conditional DB cancel that
    is a no-op when the row is already terminal — so a run whose finally
    block produced a proper terminal status keeps it, and a run that truly
    never finalized (e.g. cancel landed before block execution captured a
    ``pre_finally_status``) lands as ``canceled``. All awaits are
    exception-contained so teardown of the enclosing tool task doesn't
    surface a secondary error over the original cancellation.
    """
    run_task.cancel()
    try:
        # Shield run_task so OUR wait timeout does not send another cancel
        # through to it — the cancel we want is already pending.
        await asyncio.wait_for(asyncio.shield(run_task), timeout=5.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception:
        LOG.warning(
            "Run task raised during cancellation grace window",
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )
    try:
        await app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final(
            workflow_run_id=workflow_run_id,
        )
    except Exception:
        LOG.warning(
            "Conditional cancel write failed",
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )


def _log_detached_cleanup_failure(task: asyncio.Task) -> None:
    exc = task.exception() if task.done() and not task.cancelled() else None
    if exc is not None:
        LOG.warning("Detached cancel fallback failed", exc_info=exc)


def _extract_credential_ids_from_tool_value(value: Any) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            found.extend(_CREDENTIAL_ID_RE.findall(item))
        elif isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple, set)):
            for nested in item:
                visit(nested)
        elif hasattr(item, "model_dump"):
            try:
                visit(item.model_dump(mode="json"))
            except Exception:
                return

    visit(value)
    return list(dict.fromkeys(found))


def _extract_credential_ids_from_workflow_parameters(parameters: Any) -> list[str]:
    if not isinstance(parameters, list):
        return []

    found: list[str] = []
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue

        parameter_type = str(parameter.get("parameter_type") or "").lower()
        workflow_parameter_type = str(parameter.get("workflow_parameter_type") or "").lower()
        if parameter_type == "credential":
            found.extend(_extract_credential_ids_from_tool_value(parameter.get("credential_id")))
        elif parameter_type == "workflow" and workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID.value:
            found.extend(_extract_credential_ids_from_tool_value(parameter.get("default_value")))

    return list(dict.fromkeys(found))


def _workflow_definition_as_dict(workflow_definition: Any) -> dict[str, Any]:
    if workflow_definition is None:
        return {}
    if isinstance(workflow_definition, dict):
        return workflow_definition
    if hasattr(workflow_definition, "model_dump"):
        try:
            dumped = workflow_definition.model_dump(mode="json")
        except Exception:
            return {}
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _extract_credential_ids_from_workflow_definition(workflow_definition: Any) -> list[str]:
    definition = _workflow_definition_as_dict(workflow_definition)
    return _extract_credential_ids_from_workflow_parameters(definition.get("parameters"))


def _extract_credential_ids_from_workflow_yaml(workflow_yaml: str | None) -> list[str]:
    if not workflow_yaml:
        return []
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return []
    if not isinstance(parsed, dict):
        return []
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return []
    return _extract_credential_ids_from_workflow_parameters(workflow_definition.get("parameters"))


def _missing_credential_reference_tool_error(missing_credential_ids: list[str]) -> str:
    formatted_ids = ", ".join(f"`{credential_id}`" for credential_id in missing_credential_ids)
    id_word = "ID" if len(missing_credential_ids) == 1 else "IDs"
    was_word = "was" if len(missing_credential_ids) == 1 else "were"
    return (
        f"The credential {id_word} {formatted_ids} {was_word} not found in this organization. "
        "Stop before creating, updating, or running the workflow. Ask the user to provide/select a valid "
        "credential ID, create the credential in the Credentials UI and return with its ID, or explicitly "
        "choose an unvalidated draft workflow that will not be run until credentials are available."
    )


def _workflow_yaml_output_policy_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    tool_context = data.context
    raw_arguments = getattr(tool_context, "tool_arguments", "")
    if not raw_arguments:
        LOG.warning(
            "workflow YAML output policy guardrail received no tool arguments",
            tool_name=getattr(tool_context, "tool_name", None),
            tool_call_id=getattr(tool_context, "tool_call_id", None),
        )
    try:
        parsed_arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else {}
    except json.JSONDecodeError:
        parsed_arguments = {}
    tool_arguments = parsed_arguments if isinstance(parsed_arguments, dict) else {}
    workflow_yaml_value = tool_arguments.get("workflow_yaml")
    workflow_yaml = workflow_yaml_value if isinstance(workflow_yaml_value, str) else None
    verdict = evaluate_output_policy(
        request_policy=getattr(getattr(tool_context, "context", None), "request_policy", None),
        workflow_yaml=workflow_yaml,
        tool_arguments=tool_arguments or raw_arguments,
    )
    trace_data = output_policy_verdict_to_trace_data(
        verdict,
        surface="tool_input",
        tool_name=getattr(tool_context, "tool_name", None),
    )
    LOG.info("copilot output policy tool guardrail verdict", **trace_data)
    if not verdict.allowed:
        return ToolGuardrailFunctionOutput.reject_content(
            format_output_policy_tool_error(verdict),
            output_info=trace_data,
        )
    return ToolGuardrailFunctionOutput.allow(output_info=trace_data)


_WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL = ToolInputGuardrail(
    guardrail_function=_workflow_yaml_output_policy_guardrail,
    name="workflow_yaml_output_policy_guardrail",
)


async def _credential_ids_validation_error(credential_ids: list[str], ctx: AgentContext) -> str | None:
    if not credential_ids:
        return None
    try:
        existing_credentials = await app.DATABASE.credentials.get_credentials_by_ids(
            credential_ids,
            organization_id=ctx.organization_id,
        )
    except Exception:
        LOG.warning(
            "Copilot tool failed to validate credential IDs",
            organization_id=ctx.organization_id,
            credential_ids=credential_ids,
            exc_info=True,
        )
        return (
            "Credential ID validation failed, so the workflow cannot be created, updated, or run safely. "
            "Ask the user to provide/select a valid credential ID or explicitly choose an unvalidated draft "
            "workflow that will not be run until credentials are available."
        )

    found_ids = {credential.credential_id for credential in existing_credentials}
    missing_ids = [credential_id for credential_id in credential_ids if credential_id not in found_ids]
    if not missing_ids:
        return None
    return _missing_credential_reference_tool_error(missing_ids)


async def _credential_reference_validation_error(value: Any, ctx: AgentContext) -> str | None:
    if isinstance(value, str):
        credential_ids = _extract_credential_ids_from_workflow_yaml(value)
    else:
        credential_ids = _extract_credential_ids_from_tool_value(value)
    return await _credential_ids_validation_error(credential_ids, ctx)


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


def _maybe_clear_reconciliation_flag(copilot_ctx: Any, result: Any) -> None:
    """Clear ``pending_reconciliation_run_id`` iff the matching resolved run
    landed in a status the caller can move past: any ``is_final_excluding_canceled``
    status, or any status (including ``canceled``) when the prior exit was an
    internal per-tool-budget cancel.
    """
    pending_run_id = getattr(copilot_ctx, "pending_reconciliation_run_id", None)
    if not isinstance(pending_run_id, str) or not pending_run_id:
        return
    if not isinstance(result, dict):
        return
    data = result.get("data")
    if not isinstance(data, dict):
        return
    resolved_run_id = data.get("workflow_run_id")
    resolved_status = data.get("overall_status")
    if not isinstance(resolved_run_id, str) or resolved_run_id != pending_run_id:
        return
    if not isinstance(resolved_status, str):
        return
    is_trusted_final = WorkflowRunStatus(resolved_status).is_final_excluding_canceled()
    # ``last_failure_category_top`` reflects the prior block-running tool's outcome —
    # only ``_record_run_blocks_result`` writes it, and the reconciliation guard
    # prevents another block-running call from clobbering it before this read.
    was_per_tool_budget = getattr(copilot_ctx, "last_failure_category_top", None) == PER_TOOL_BUDGET_FAILURE_CATEGORY
    if is_trusted_final or was_per_tool_budget:
        copilot_ctx.pending_reconciliation_run_id = None


def _enum_or_string_name(value: Any) -> str:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        raw = getattr(value, "name", raw)
    return str(raw).strip().lower()


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
    setattr(copilot_ctx, "per_tool_budget_problem_block_labels", sorted(labels))


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
    setattr(copilot_ctx, "per_tool_budget_problem_block_labels", remaining)


def _requested_block_label_set(arguments: dict[str, Any] | None) -> set[str] | None:
    if not isinstance(arguments, dict):
        return None
    block_labels = arguments.get("block_labels")
    if not isinstance(block_labels, list):
        return None
    return {label for label in block_labels if isinstance(label, str) and label}


def _per_tool_budget_problem_rerun_error(ctx: Any, arguments: dict[str, Any] | None) -> str | None:
    problem_labels = _per_tool_budget_problem_label_set(ctx)
    if not problem_labels:
        return None

    requested_labels = _requested_block_label_set(arguments)
    blocked_labels = problem_labels if not requested_labels else problem_labels & requested_labels
    if not blocked_labels:
        return None

    labels = ", ".join(sorted(blocked_labels))
    return (
        "The prior PER_TOOL_BUDGET run's get_run_results showed navigation "
        f"block(s) [{labels}] were canceled or failed while applying page state. "
        "Do NOT rerun those block label(s) unchanged with run_blocks_and_collect_debug. "
        "Update the workflow to split or replace the oversized navigation block first, "
        "prefer a code or validation block for DOM/page-state verification, or run only "
        "newly-created smaller block labels that apply one missing constraint at a time."
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


def _is_meaningful_extracted_data(extracted: Any) -> bool:
    """Return True when extracted data contains at least one non-null, non-empty value.

    A dict like ``{"price": None}`` is technically present but carries no signal —
    treat it the same as no output at all so enforcement can nudge the agent to
    investigate instead of declaring success.
    """
    if extracted is None:
        return False
    if isinstance(extracted, (str, bytes)):
        return bool(extracted)
    if isinstance(extracted, dict):
        return any(_is_meaningful_extracted_data(v) for v in extracted.values())
    if isinstance(extracted, (list, tuple, set)):
        return any(_is_meaningful_extracted_data(v) for v in extracted)
    # Numbers, booleans, and other scalars count as meaningful output.
    return True


# Payload fields inside a ``TaskOutput.from_task()`` envelope
# (schemas/tasks.py:TaskOutput). Only these carry "did the block produce
# something useful?" signal; the rest (task_id, status, artifact IDs, etc.)
# are always populated on a completed run and would short-circuit
# _is_meaningful_extracted_data to True even when nothing useful was produced.
_TASK_OUTPUT_PAYLOAD_FIELDS: tuple[str, ...] = (
    "extracted_information",
    "downloaded_files",
    "downloaded_file_urls",
)


def _block_data_payload(extracted_data: Any, block_type: str | None) -> Any:
    """Return the payload view of a block's output for the meaningful-data check.

    For task-envelope block types (``_TASK_ENVELOPE_BLOCK_TYPES``), slice the
    envelope down to ``_TASK_OUTPUT_PAYLOAD_FIELDS`` so envelope metadata
    can't mask an empty result. Other data-producing types pass through
    unchanged — e.g. TEXT_PROMPT's ``block.output`` is the raw LLM response
    dict (TextPromptBlock.execute records ``output_parameter_value=response``
    directly), so scoping the unwrap avoids slicing a user-defined
    json_schema that happens to include an ``extracted_information`` field.
    """
    if block_type in _TASK_ENVELOPE_BLOCK_TYPES and isinstance(extracted_data, dict):
        return {field: extracted_data.get(field) for field in _TASK_OUTPUT_PAYLOAD_FIELDS}
    return extracted_data


async def _attach_action_traces(
    blocks: list,
    results: list[dict[str, Any]],
    organization_id: str,
) -> None:
    """For non-success blocks with a task_id, fetch and attach a compact action trace."""
    failed_task_ids = [
        b.task_id for b, r in zip(blocks, results) if b.task_id and r.get("status") in _FAILED_BLOCK_STATUSES
    ]
    if not failed_task_ids:
        return

    rows = await app.DATABASE.tasks.get_recent_actions_for_tasks(
        task_ids=failed_task_ids,
        organization_id=organization_id,
    )

    actions_by_task: dict[str, list] = defaultdict(list)
    for row in rows:
        if row.task_id is not None:
            actions_by_task[row.task_id].append(row)

    for block, block_result in zip(blocks, results):
        if block_result.get("status") not in _FAILED_BLOCK_STATUSES or not block.task_id:
            continue
        task_actions = actions_by_task.get(block.task_id, [])
        block_result["action_trace"] = [
            {
                "action": a.action_type,
                "status": a.status,
                "reasoning": a.reasoning[:150] if a.reasoning else None,
                "element": a.element_id,
            }
            for a in task_actions
        ]


async def _fetch_last_screenshot_b64(task_id: str, organization_id: str) -> str | None:
    try:
        artifacts = await app.DATABASE.artifacts.get_artifacts_for_task_v2(
            task_v2_id=task_id,
            organization_id=organization_id,
            artifact_types=[ArtifactType.SCREENSHOT_LLM],
        )
        if not artifacts:
            return None
        # The last artifact is the one captured closest to the failure.
        artifact_bytes = await app.ARTIFACT_MANAGER.retrieve_artifact(artifacts[-1])
        if not artifact_bytes:
            return None
        return base64.b64encode(artifact_bytes).decode("utf-8")
    except Exception:
        LOG.debug("Failed to fetch screenshot for failed block", task_id=task_id, exc_info=True)
        return None


async def _attach_failed_block_screenshots(
    blocks: list,
    results: list[dict[str, Any]],
    organization_id: str,
) -> None:
    """For failed blocks with a task_id, fetch the last SCREENSHOT_LLM artifact."""
    task_id_to_block: dict[str, dict] = {
        block.task_id: block_result
        for block, block_result in zip(blocks, results)
        if block.task_id and block_result.get("status") in _FAILED_BLOCK_STATUSES
    }
    if not task_id_to_block:
        return

    task_ids = list(task_id_to_block.keys())
    screenshots = await asyncio.gather(
        *(_fetch_last_screenshot_b64(task_id, organization_id) for task_id in task_ids),
    )
    for task_id, b64 in zip(task_ids, screenshots):
        if b64 is not None:
            task_id_to_block[task_id]["screenshot_b64"] = b64


BLOCK_RUNNING_TOOLS = frozenset({"run_blocks_and_collect_debug", "update_and_run_blocks"})


def _copilot_seconds_remaining(ctx: AgentContext) -> float | None:
    started_at = getattr(ctx, "copilot_run_start_monotonic", None)
    if not isinstance(started_at, int | float):
        return None
    return TOTAL_TIMEOUT_SECONDS - (time.monotonic() - float(started_at))


def _active_block_run_budget_seconds(ctx: AgentContext) -> int:
    remaining = _copilot_seconds_remaining(ctx)
    if remaining is None:
        return PER_TOOL_CALL_BUDGET_SECONDS
    remaining_after_reply_reserve = remaining - COPILOT_FINAL_REPLY_RESERVE_SECONDS
    return max(1, min(PER_TOOL_CALL_BUDGET_SECONDS, int(remaining_after_reply_reserve)))


def _late_block_running_call_error(ctx: AgentContext) -> str | None:
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
        return (
            f"Wall-clock budget too low to retry: about {int(max(0.0, remaining))}s remain of the "
            f"{TOTAL_TIMEOUT_SECONDS}s session budget. A verified workflow exists from before the failure. "
            "Do NOT call update_and_run_blocks or run_blocks_and_collect_debug again. REPLY now: summarize "
            "what worked, name the block that failed, and tell the user they can keep the verified prefix or discard."
        )

    if isinstance(last_failed_workflow_yaml, str) and last_failed_workflow_yaml:
        return (
            f"Less than {COPILOT_FINAL_REPLY_RESERVE_SECONDS} seconds remain in this Copilot turn "
            "after the previous workflow run failed. Do NOT retry block-running tools; reply to the user "
            "with the failure evidence gathered so far and make clear that any draft workflow is unverified."
        )

    return (
        f"Less than {COPILOT_FINAL_REPLY_RESERVE_SECONDS} seconds remain in this Copilot turn. "
        "Do NOT start another block-running tool call; reply to the user with the workflow draft and "
        "progress gathered so far, and make clear which parts have not been verified end-to-end."
    )


def _tool_loop_error(ctx: AgentContext, tool_name: str, arguments: dict[str, Any] | None = None) -> str | None:
    detected = detect_failed_tool_step_loop_for_ctx(ctx, tool_name, arguments or {})
    if detected is not None:
        return detected

    # Consecutive same-name guard: false-positives on the intended iterative
    # build (one new block per update_and_run_blocks). Block-running tools
    # rely on the progress-aware checks below instead.
    tracker = getattr(ctx, "consecutive_tool_tracker", None)
    if isinstance(tracker, list) and tool_name not in BLOCK_RUNNING_TOOLS:
        detected = detect_tool_loop(tracker, tool_name)
        if detected is not None:
            return detected

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
            return (
                f"The previous block-running tool call for run {pending_run_id} "
                f"ended without a trustworthy terminal status. "
                f'Call `get_run_results(workflow_run_id="{pending_run_id}")` '
                f"first, report the result to the user, then await user input "
                f"before running more blocks. This guard prevents duplicate "
                f"side effects on live sites."
            )

        if tool_name == "run_blocks_and_collect_debug":
            budget_rerun_error = _per_tool_budget_problem_rerun_error(ctx, arguments)
            if budget_rerun_error is not None:
                return budget_rerun_error

        streak_raw = getattr(ctx, "repeated_action_fingerprint_streak_count", 0)
        streak = streak_raw if isinstance(streak_raw, int) else 0
        if streak >= REPEATED_ACTION_STREAK_ABORT_AT:
            return (
                f"Repeated-action abort: the last {streak} runs fired the same "
                "action sequence against the page without making progress. "
                "The site is likely blocked by a captcha, popup, anti-bot "
                "challenge, or hidden validation error that the agent is not "
                "detecting. Do NOT retry this tool — conclude the workflow is "
                "not automatable as-is and report back to the user."
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
            return (
                f"Prior run in this turn hit a permanent navigation error "
                f"({prior_nav_error[:200]}). Do NOT retry — the URL is unreachable "
                "regardless of subdomain or path variations. Reply to the user "
                "explaining the failure and asking them to verify the URL."
            )

        late_block_running_error = _late_block_running_call_error(ctx)
        if late_block_running_error is not None:
            return late_block_running_error
    return None


def _request_policy_tool_error(ctx: AgentContext, tool_name: str) -> str | None:
    policy = getattr(ctx, "request_policy", None)
    if not isinstance(policy, RequestPolicy):
        return None
    if tool_name == "update_workflow" and not policy.allow_update_workflow:
        return (
            "Request policy blocks workflow updates for the latest user message. "
            "Ask the user for safe stored credential metadata instead."
        )
    if tool_name in BLOCK_RUNNING_TOOLS and not policy.allow_run_blocks:
        if policy.testing_intent == "skip_test":
            return "Request policy says the latest user message asked for an untested draft. Use update_workflow only."
        return (
            "Request policy blocks block-running tools for the latest user message. "
            "Ask the user for the required safe credential or clarification before testing."
        )
    return None


_PARAMETER_TYPE_PLACEHOLDERS: dict[WorkflowParameterType, Any] = {
    WorkflowParameterType.STRING: "",
    WorkflowParameterType.INTEGER: 0,
    WorkflowParameterType.FLOAT: 0.0,
    WorkflowParameterType.BOOLEAN: False,
    WorkflowParameterType.JSON: {},
    WorkflowParameterType.FILE_URL: "",
}


def _placeholder_for_parameter_type(param_type: WorkflowParameterType) -> Any:
    return _PARAMETER_TYPE_PLACEHOLDERS.get(param_type)


def _parameter_binding_invariant_error(
    workflow: Workflow,
    persisted_workflow_params: list[WorkflowParameter],
    persisted_output_params: list[OutputParameter],
) -> tuple[str, dict[str, list[str]], dict[str, list[str]]] | None:
    """Return a ``(summary, missing_persisted, missing_from_definition)`` tuple
    when ``workflow.workflow_definition`` disagrees with persisted
    definition-parameter rows for runtime-relevant classes. Returns ``None``
    when aligned.

    Compares ``WorkflowParameter`` rows by ``(key, workflow_parameter_type)``
    and ``OutputParameter`` rows by ``key``. Secret/credential and context
    parameters are intentionally out of scope — runtime reads those from the
    definition JSON.
    """
    definition = getattr(workflow, "workflow_definition", None)
    parameters = getattr(definition, "parameters", None) if definition else None
    parameters = list(parameters) if parameters else []

    def_workflow_ids: set[tuple[str, str]] = set()
    def_output_keys: set[str] = set()
    for parameter in parameters:
        if isinstance(parameter, WorkflowParameter):
            def_workflow_ids.add((parameter.key, parameter.workflow_parameter_type.value))
        elif isinstance(parameter, OutputParameter):
            def_output_keys.add(parameter.key)

    persisted_workflow_ids: set[tuple[str, str]] = {
        (wp.key, wp.workflow_parameter_type.value) for wp in persisted_workflow_params
    }
    persisted_output_keys: set[str] = {op.key for op in persisted_output_params}

    missing_persisted_workflow = sorted(
        f"{key} ({ptype})" for (key, ptype) in def_workflow_ids - persisted_workflow_ids
    )
    extra_persisted_workflow = sorted(f"{key} ({ptype})" for (key, ptype) in persisted_workflow_ids - def_workflow_ids)
    missing_persisted_output = sorted(def_output_keys - persisted_output_keys)
    extra_persisted_output = sorted(persisted_output_keys - def_output_keys)

    if (
        not missing_persisted_workflow
        and not extra_persisted_workflow
        and not missing_persisted_output
        and not extra_persisted_output
    ):
        return None

    summary = (
        "Pre-run invariant: workflow_definition and persisted parameter rows disagree. "
        f"workflow missing persisted: {missing_persisted_workflow or '[]'}; "
        f"workflow missing from definition: {extra_persisted_workflow or '[]'}; "
        f"output missing persisted: {missing_persisted_output or '[]'}; "
        f"output missing from definition: {extra_persisted_output or '[]'}"
    )
    return (
        summary,
        {"workflow": missing_persisted_workflow, "output": missing_persisted_output},
        {"workflow": extra_persisted_workflow, "output": extra_persisted_output},
    )


async def _update_workflow(
    params: dict[str, Any],
    ctx: AgentContext,
    *,
    allow_missing_credentials: bool | None = None,
) -> dict[str, Any]:
    policy_error = _request_policy_tool_error(ctx, "update_workflow")
    if policy_error is not None:
        return {"ok": False, "error": policy_error}

    workflow_yaml = params["workflow_yaml"]
    if allow_missing_credentials is None:
        allow_missing_credentials = getattr(ctx, "allow_untested_workflow_draft", False) is True
    if not allow_missing_credentials:
        credential_error = await _credential_reference_validation_error(workflow_yaml, ctx)
        if credential_error is not None:
            return {"ok": False, "error": credential_error}

    output_policy_verdict = evaluate_output_policy(
        request_policy=getattr(ctx, "request_policy", None),
        workflow_yaml=workflow_yaml,
        tool_arguments=params,
    )
    if not output_policy_verdict.allowed:
        LOG.info(
            "copilot output policy tool body verdict",
            **output_policy_verdict_to_trace_data(
                output_policy_verdict,
                surface="tool_body",
                tool_name="update_workflow",
            ),
        )
        return {"ok": False, "error": format_output_policy_tool_error(output_policy_verdict)}

    # Prefer the most-recent in-turn emission so cross-path flows (inline
    # REPLACE_WORKFLOW followed by update_workflow) compare against what the
    # LLM actually saw, not the turn-start persisted state.
    last_yaml = getattr(ctx, "last_workflow_yaml", None)
    prior_yaml = last_yaml if isinstance(last_yaml, str) and last_yaml else ctx.workflow_yaml
    stale_metadata = _detect_stale_block_metadata(workflow_yaml, prior_yaml)
    if stale_metadata:
        return {"ok": False, "error": _stale_block_metadata_message(stale_metadata)}

    wait_block_error = _timing_only_challenge_wait_reject_message(ctx, workflow_yaml)
    if wait_block_error:
        return {"ok": False, "error": wait_block_error}

    # Post-emission reject of copilot-v2 writes that introduce a banned
    # block type. The schema pre_hook only fires when the LLM consults the
    # schema; this safety net fires regardless of emission path. Label-based
    # diff preserves legacy workflows — only NEW banned labels trip the reject.
    banned_items = _detect_new_banned_blocks(workflow_yaml, ctx.workflow_yaml)
    if banned_items:
        _record_banned_block_reject_span("_update_workflow", banned_items)
        return {"ok": False, "error": _banned_block_reject_message(banned_items)}

    goto_url_state_shortcuts = _detect_unverified_goto_url_state_shortcuts(workflow_yaml)
    if goto_url_state_shortcuts:
        _record_goto_url_state_shortcut_reject_span("_update_workflow", goto_url_state_shortcuts)
        return {"ok": False, "error": _goto_url_state_shortcut_message(goto_url_state_shortcuts)}

    navigation_url_search_setups = _detect_navigation_url_stable_search_setups(workflow_yaml)
    if navigation_url_search_setups:
        _record_navigation_url_stable_search_reject_span("_update_workflow", navigation_url_search_setups)
        return {"ok": False, "error": _navigation_url_stable_search_message(navigation_url_search_setups)}

    stable_search_drips = _detect_click_driven_stable_search_drips(workflow_yaml)
    if stable_search_drips:
        _record_goto_url_stable_search_drip_reject_span("_update_workflow", stable_search_drips)
        return {"ok": False, "error": _goto_url_stable_search_drip_message(stable_search_drips)}
    try:
        workflow = _process_workflow_yaml(
            workflow_id=ctx.workflow_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
            workflow_yaml=workflow_yaml,
        )
        _record_workflow_proxy_location_span(workflow_yaml, workflow)

        created_by_stamp = await resolve_copilot_created_by_stamp(ctx.workflow_id, ctx.organization_id)

        await app.WORKFLOW_SERVICE.update_workflow_definition(
            workflow_id=ctx.workflow_id,
            organization_id=ctx.organization_id,
            title=workflow.title,
            description=workflow.description,
            workflow_definition=workflow.workflow_definition,
            proxy_location=workflow.proxy_location,
            webhook_callback_url=workflow.webhook_callback_url,
            persist_browser_session=workflow.persist_browser_session,
            model=workflow.model,
            max_screenshot_scrolling_times=workflow.max_screenshot_scrolls,
            extra_http_headers=workflow.extra_http_headers,
            run_with=workflow.run_with,
            ai_fallback=workflow.ai_fallback,
            cache_key=workflow.cache_key,
            run_sequentially=workflow.run_sequentially,
            sequential_key=workflow.sequential_key,
            created_by=created_by_stamp,
            edited_by="copilot",
        )
        ctx.workflow_yaml = workflow_yaml
        return {
            "ok": True,
            "data": {
                "message": "Workflow updated successfully.",
                "block_count": len(workflow.workflow_definition.blocks) if workflow.workflow_definition else 0,
            },
            "_workflow": workflow,
        }
    except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException) as e:
        return {
            "ok": False,
            "error": f"Workflow validation failed: {e}",
        }


async def _list_credentials(params: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
    page = params.get("page", 1)
    page_size = min(params.get("page_size", 10), 50)
    credentials = await app.DATABASE.credentials.get_credentials(
        organization_id=ctx.organization_id,
        page=page,
        page_size=page_size,
    )
    serialized = []
    for cred in credentials:
        entry: dict[str, Any] = {
            "credential_id": cred.credential_id,
            "name": cred.name,
            "credential_type": str(cred.credential_type),
        }
        if cred.username:
            entry["username"] = cred.username
            entry["totp_type"] = str(cred.totp_type) if cred.totp_type else None
        elif cred.card_last4:
            entry["card_last_four"] = cred.card_last4
            entry["card_brand"] = cred.card_brand
        elif cred.secret_label:
            entry["secret_label"] = cred.secret_label
        serialized.append(entry)
    return {
        "ok": True,
        "data": {
            "credentials": serialized,
            "page": page,
            "page_size": page_size,
            "count": len(serialized),
            "has_more": len(serialized) == page_size,
        },
    }


# Block types that establish browser state (loaded page / authenticated
# session / navigation target). These are valid upstream anchors to walk back
# to when a downstream edit invalidates part of the chain.
#
# We intentionally do NOT maintain a companion "rerunnable from current
# browser state" set. We have no signal that the persistent browser session
# is actually anchored at the frontier boundary — after a successful
# [A, B, C] the browser is at post-C state, not pre-C — so rerunning only
# an edited block is unsafe even for read-only types. Every edit walks back
# to an upstream state-establisher, or falls back to the full requested list.
_BLOCK_TYPES_STATE_ESTABLISHER = frozenset({"navigation", "login", "goto_url"})

_JINJA_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_OUTPUT_REF_RE = re.compile(rf"\{{\{{\s*({_JINJA_IDENTIFIER})_output\s*(?=[\.|}}])")
_BLOCK_FORM_REF_RE = re.compile(rf"\{{\{{\s*({_JINJA_IDENTIFIER})\s*\.")
_JINJA_ROOT_RE = re.compile(rf"\{{\{{\s*({_JINJA_IDENTIFIER})\s*(?=[\.|}}])")

_JINJA_RUNTIME_GLOBAL_ROOTS = frozenset(SandboxedEnvironment().globals)
_JINJA_LITERAL_ROOTS = frozenset({"none", "true", "false"})
_JINJA_SPECIAL_CONTEXT_ROOTS = frozenset({"loop", "self", "varargs", "kwargs"})
_SKYVERN_TEMPLATE_CONTEXT_ROOTS = frozenset(RESERVED_PARAMETER_KEYS) | frozenset(
    {
        "parameters",
        "browser_session_id",
        "organization_id",
        # Conditional / branch evaluation roots — see BranchEvaluationContext.build_template_data.
        "params",
        "outputs",
        "environment",
        "env",
        "llm",
    }
)
_TEMPLATE_BUILTIN_ROOTS = (
    _JINJA_RUNTIME_GLOBAL_ROOTS | _JINJA_LITERAL_ROOTS | _JINJA_SPECIAL_CONTEXT_ROOTS | _SKYVERN_TEMPLATE_CONTEXT_ROOTS
)

# Keep this to grammatical glue only. Workflow/action words are intentionally
# not filtered; the two-token stale threshold is the conservative guardrail.
_BLOCK_METADATA_STOPWORDS = frozenset({"and", "for", "the", "with"})


def _block_type_name(block: Any) -> str:
    """Lowercase string name of a block's type, for both YAML and runtime blocks."""
    bt = getattr(block, "block_type", None)
    if bt is None:
        return ""
    name = getattr(bt, "value", None) or getattr(bt, "name", None) or str(bt)
    return str(name).lower()


def _blocks_by_label(workflow_definition: Any) -> dict[str, Any]:
    blocks = getattr(workflow_definition, "blocks", None) if workflow_definition else None
    by_label: dict[str, Any] = {}
    if not blocks:
        return by_label
    for block in blocks:
        label = getattr(block, "label", None)
        if isinstance(label, str):
            by_label[label] = block
    return by_label


# Minimum length to apply the trailing-``s`` plural strip; below this we
# leave the token alone so words like ``is``/``us``/``has`` aren't mangled.
_MIN_STEMMABLE_TOKEN_LEN = 5


def _metadata_token(token: str) -> str:
    token = token.lower()
    if len(token) >= _MIN_STEMMABLE_TOKEN_LEN and token.endswith("s"):
        token = token[:-1]
    return token


def _metadata_tokens(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    tokens: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9]+", value):
        normalized = _metadata_token(token)
        if len(normalized) <= 2 or normalized in _BLOCK_METADATA_STOPWORDS:
            continue
        tokens.add(normalized)
    return tokens


def _iter_yaml_blocks(blocks: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if not isinstance(blocks, list):
        return found
    for block in blocks:
        if not isinstance(block, dict):
            continue
        found.append(block)
        loop_blocks = block.get("loop_blocks")
        if isinstance(loop_blocks, list):
            found.extend(_iter_yaml_blocks(loop_blocks))
    return found


def _workflow_yaml_blocks_by_label(workflow_yaml: str | None) -> dict[str, dict[str, Any]]:
    if not workflow_yaml:
        return {}
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return {}
    by_label: dict[str, dict[str, Any]] = {}
    for block in _iter_yaml_blocks(workflow_definition.get("blocks")):
        label = block.get("label")
        if isinstance(label, str):
            by_label[label] = block
    return by_label


def _semantic_tokens_from_yaml(value: Any, *, exclude_keys: frozenset[str]) -> set[str]:
    tokens: set[str] = set()
    if isinstance(value, str):
        return _metadata_tokens(value)
    if isinstance(value, list):
        for item in value:
            tokens.update(_semantic_tokens_from_yaml(item, exclude_keys=exclude_keys))
        return tokens
    if isinstance(value, dict):
        for key, item in value.items():
            if key in exclude_keys:
                continue
            tokens.update(_semantic_tokens_from_yaml(item, exclude_keys=exclude_keys))
    return tokens


def _stale_metadata_reason(
    *,
    field_name: str,
    field_value: Any,
    prior_block: dict[str, Any],
    submitted_block: dict[str, Any],
    current_exclude_keys: frozenset[str],
) -> str | None:
    tokens = _metadata_tokens(field_value)
    if len(tokens) < 2:
        return None

    prior_tokens = _semantic_tokens_from_yaml(prior_block, exclude_keys=current_exclude_keys)
    current_tokens = _semantic_tokens_from_yaml(submitted_block, exclude_keys=current_exclude_keys)
    removed_tokens = prior_tokens - current_tokens
    if len(tokens & removed_tokens) < 2:
        return None

    return f"{field_name} {field_value!r} appears stale"


def _prior_blocks_by_unique_title(prior_by_label: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    titled = [(b["title"], b) for b in prior_by_label.values() if isinstance(b.get("title"), str) and b["title"]]
    counts = Counter(title for title, _ in titled)
    return {title: block for title, block in titled if counts[title] == 1}


_STALE_BASE_EXCLUDE = frozenset({"label", "next_block_label"})
_STALE_TITLE_EXCLUDE = frozenset({"label", "title", "next_block_label"})


def _stale_for_renamed_label(
    label: str,
    submitted_block: dict[str, Any],
    prior_by_unique_title: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    submitted_title = submitted_block.get("title")
    # Title-less blocks intentionally skip the renamed-label path: with no
    # title there is no cross-reference to a prior block, so we'd be
    # guessing whether the rename was warranted.
    if not (isinstance(submitted_title, str) and submitted_title):
        return None
    prior_block = prior_by_unique_title.get(submitted_title)
    if prior_block is None:
        return None
    title_reason = _stale_metadata_reason(
        field_name="title",
        field_value=submitted_title,
        prior_block=prior_block,
        submitted_block=submitted_block,
        current_exclude_keys=_STALE_TITLE_EXCLUDE,
    )
    if not title_reason:
        return None
    return {"label": label, "reasons": [title_reason]}


def _stale_for_matched_label(
    label: str,
    submitted_block: dict[str, Any],
    prior_block: dict[str, Any],
) -> dict[str, Any] | None:
    reasons: list[str] = []
    submitted_title = submitted_block.get("title")
    prior_title = prior_block.get("title")
    title_reason = None
    if isinstance(submitted_title, str) and submitted_title and submitted_title == prior_title:
        title_reason = _stale_metadata_reason(
            field_name="title",
            field_value=submitted_title,
            prior_block=prior_block,
            submitted_block=submitted_block,
            current_exclude_keys=_STALE_TITLE_EXCLUDE,
        )
        if title_reason:
            reasons.append(title_reason)

    # When the title was already flagged stale, exclude its tokens from the
    # label-stale comparison so the same words don't double-count; otherwise
    # the title's tokens are stable content that should weigh in.
    label_exclude_keys = _STALE_TITLE_EXCLUDE if title_reason else _STALE_BASE_EXCLUDE
    label_reason = _stale_metadata_reason(
        field_name="label",
        field_value=label,
        prior_block=prior_block,
        submitted_block=submitted_block,
        current_exclude_keys=label_exclude_keys,
    )
    if label_reason:
        reasons.insert(0, label_reason)

    if not reasons:
        return None
    return {"label": label, "reasons": reasons}


def _detect_stale_block_metadata(submitted_yaml: str | None, prior_yaml: str | None) -> list[dict[str, Any]]:
    """Find corrected blocks whose old label/title no longer matches their revised goal text."""
    prior_by_label = _workflow_yaml_blocks_by_label(prior_yaml)
    submitted_by_label = _workflow_yaml_blocks_by_label(submitted_yaml)
    if not prior_by_label or not submitted_by_label:
        return []

    prior_by_unique_title = _prior_blocks_by_unique_title(prior_by_label)

    stale_items: list[dict[str, Any]] = []
    for label, submitted_block in submitted_by_label.items():
        prior_block = prior_by_label.get(label)
        if prior_block is None:
            item = _stale_for_renamed_label(label, submitted_block, prior_by_unique_title)
        else:
            item = _stale_for_matched_label(label, submitted_block, prior_block)
        if item is not None:
            stale_items.append(item)
    return stale_items


_STALE_BLOCK_METADATA_MESSAGE_LIMIT = 5


def _stale_block_metadata_message(items: list[dict[str, Any]]) -> str:
    details = []
    for item in items[:_STALE_BLOCK_METADATA_MESSAGE_LIMIT]:
        label = item.get("label", "?")
        reasons = item.get("reasons") or []
        detail = "; ".join(str(reason) for reason in reasons)
        details.append(f"{label}: {detail}")
    if len(items) > _STALE_BLOCK_METADATA_MESSAGE_LIMIT:
        details.append(f"(and {len(items) - _STALE_BLOCK_METADATA_MESSAGE_LIMIT} more)")
    joined = "; ".join(details)
    return (
        "Workflow validation failed: corrected block metadata still appears stale. "
        "When changing a user's requested subject, URL, or action, rename affected block labels and titles "
        "to match the revised goal, and update next_block_label, block_labels, and Jinja references accordingly. "
        f"Stale metadata: {joined}"
    )


def _find_invalidated_labels(
    old_definition: Any,
    new_definition: Any,
    requested_labels: list[str],
) -> set[str]:
    """Return the set of requested labels whose behavior is invalidated.

    A label is invalidated when its own config changed or when any upstream
    label in the requested chain was invalidated (downstream trust propagates
    forward).
    """
    old_by_label = _blocks_by_label(old_definition)
    new_by_label = _blocks_by_label(new_definition)
    invalidated: set[str] = set()
    upstream_invalidated = False
    for label in requested_labels:
        if upstream_invalidated:
            invalidated.add(label)
            continue
        old_block = old_by_label.get(label)
        new_block = new_by_label.get(label)
        if old_block is None or new_block is None:
            invalidated.add(label)
            upstream_invalidated = True
            continue
        if _canonical_block_config(old_block) != _canonical_block_config(new_block):
            invalidated.add(label)
            upstream_invalidated = True
    return invalidated


def _earliest_invalidated(requested_labels: list[str], invalidated: set[str]) -> str | None:
    for label in requested_labels:
        if label in invalidated:
            return label
    return None


def _nearest_upstream_state_establisher(
    requested_labels: list[str], target_label: str, new_definition: Any
) -> str | None:
    by_label = _blocks_by_label(new_definition)
    try:
        idx = requested_labels.index(target_label)
    except ValueError:
        return None
    for candidate in reversed(requested_labels[:idx]):
        block = by_label.get(candidate)
        if block is None:
            continue
        if _block_type_name(block) in _BLOCK_TYPES_STATE_ESTABLISHER:
            return candidate
    return None


def _serialized_frontier_block_configs(frontier_labels: list[str], new_definition: Any) -> list[str]:
    by_label = _blocks_by_label(new_definition)
    serialized_configs: list[str] = []
    for label in frontier_labels:
        block = by_label.get(label)
        if block is None:
            continue
        try:
            serialized_configs.append(json.dumps(_canonical_block_config(block), default=str, separators=(",", ":")))
        except (TypeError, ValueError):
            serialized_configs.append(repr(block))
    return serialized_configs


def _workflow_parameter_keys(definition: Any) -> set[str]:
    parameters = getattr(definition, "parameters", None) if definition else None
    keys: set[str] = set()
    if not parameters:
        return keys
    for parameter in parameters:
        key = getattr(parameter, "key", None)
        if isinstance(key, str):
            keys.add(key)
    return keys


_CREDENTIAL_REAL_VALUE_SUFFIXES = ("_real_username", "_real_password")


def _classify_frontier_jinja_refs(
    frontier_labels: list[str],
    new_definition: Any,
    serialized_configs: list[str] | None = None,
) -> tuple[set[str], set[str], set[str]]:
    """Single pass over frontier blocks; returns ``(suffix_form_refs, block_form_refs, unknown_roots)``."""
    if serialized_configs is None:
        serialized_configs = _serialized_frontier_block_configs(frontier_labels, new_definition)
    known_labels = set(_blocks_by_label(new_definition))
    parameter_keys = _workflow_parameter_keys(new_definition)
    known_roots = known_labels | parameter_keys | _TEMPLATE_BUILTIN_ROOTS

    suffix_form_refs: set[str] = set()
    block_form_refs: set[str] = set()
    unknown_roots: set[str] = set()

    for serialized in serialized_configs:
        for match in _OUTPUT_REF_RE.findall(serialized):
            if match in known_labels:
                suffix_form_refs.add(match)
        for match in _BLOCK_FORM_REF_RE.findall(serialized):
            if match in known_labels:
                block_form_refs.add(match)
        for root in _JINJA_ROOT_RE.findall(serialized):
            if root in known_roots:
                continue
            if root.endswith("_output") and root[: -len("_output")] in known_labels:
                continue
            if any(
                root.endswith(suffix) and root[: -len(suffix)] in parameter_keys
                for suffix in _CREDENTIAL_REAL_VALUE_SUFFIXES
            ):
                continue
            unknown_roots.add(root)

    return suffix_form_refs, block_form_refs, unknown_roots


def _referenced_output_labels(
    frontier_labels: list[str],
    new_definition: Any,
    serialized_configs: list[str] | None = None,
) -> set[str]:
    suffix_refs, block_form_refs, _ = _classify_frontier_jinja_refs(frontier_labels, new_definition, serialized_configs)
    return suffix_refs | block_form_refs


def _block_form_output_labels(
    frontier_labels: list[str],
    new_definition: Any,
    serialized_configs: list[str] | None = None,
) -> set[str]:
    _, block_form_refs, _ = _classify_frontier_jinja_refs(frontier_labels, new_definition, serialized_configs)
    return block_form_refs


def _unknown_jinja_roots(
    frontier_labels: list[str],
    new_definition: Any,
    serialized_configs: list[str] | None = None,
) -> set[str]:
    _, _, unknown_roots = _classify_frontier_jinja_refs(frontier_labels, new_definition, serialized_configs)
    return unknown_roots


def _summarize_action_trace(action_trace: list[dict[str, Any]] | None) -> list[str]:
    """Compact, stringified summary of action entries for the compact packet."""
    if not action_trace:
        return []
    summary: list[str] = []
    for entry in action_trace[-6:]:
        if not isinstance(entry, dict):
            continue
        action = entry.get("action") or "?"
        status = entry.get("status") or ""
        element = entry.get("element")
        bits = [str(action)]
        if element:
            bits.append(str(element))
        if status:
            bits.append(str(status))
        summary.append(" ".join(bits).strip())
    return summary


async def _get_prior_workflow_definition(ctx: AgentContext) -> Any:
    """Hybrid: prefer ctx.last_workflow, fall back to DB fetch on cold start."""
    last_workflow = getattr(ctx, "last_workflow", None)
    if last_workflow is not None:
        definition = getattr(last_workflow, "workflow_definition", None)
        if definition is not None:
            return definition
    last_yaml = getattr(ctx, "last_workflow_yaml", None)
    if last_yaml:
        try:
            workflow = _process_workflow_yaml(
                workflow_id=ctx.workflow_id,
                workflow_permanent_id=ctx.workflow_permanent_id,
                organization_id=ctx.organization_id,
                workflow_yaml=last_yaml,
            )
            return workflow.workflow_definition
        except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException):
            pass
    try:
        fetched = await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
        )
        if fetched is not None:
            return fetched.workflow_definition
    except Exception:
        LOG.debug("Failed to fetch prior workflow definition for frontier diff", exc_info=True)
    return None


def _plan_frontier(
    ctx: Any,
    requested_labels: list[str],
    old_definition: Any,
    new_definition: Any,
) -> tuple[list[str], dict[str, Any], str | None]:
    """Plan the frontier execution.

    Returns ``(labels_to_execute, block_outputs_to_seed, frontier_start_label)``.

    Falls back to the full requested list on any ambiguity. When there is no
    workflow change (plain run path) the frontier is the first requested label
    and we seed any verified outputs referenced by the suffix.
    """
    if not requested_labels:
        return requested_labels, {}, None
    if new_definition is None:
        return requested_labels, {}, requested_labels[0]

    verified_outputs: dict[str, Any] = dict(getattr(ctx, "verified_block_outputs", {}) or {})
    verified_prefix: list[str] = list(getattr(ctx, "verified_prefix_labels", []) or [])
    verified_prefix_set = set(verified_prefix)

    # No old definition (cold start or parse failure) OR no diff signal → plain path.
    if old_definition is None:
        frontier = requested_labels[0]
        return _seed_for_frontier(requested_labels, frontier, verified_outputs, new_definition)

    try:
        invalidated = _find_invalidated_labels(old_definition, new_definition, requested_labels)
    except Exception:
        LOG.debug("Frontier diff failed, falling back to full run", exc_info=True)
        return requested_labels, {}, requested_labels[0]

    earliest = _earliest_invalidated(requested_labels, invalidated)
    if earliest is None:
        # No invalidation at all — unchanged request. Use first requested as frontier.
        frontier = requested_labels[0]
        return _seed_for_frontier(requested_labels, frontier, verified_outputs, new_definition)

    # Ensure the prefix before the earliest invalidated label is all in the
    # verified prefix from a successful prior run. Otherwise we have no
    # trusted anchor — fall back to the full requested list.
    prefix_in_requested = [label for label in requested_labels if label != earliest]
    prefix_in_requested = prefix_in_requested[: requested_labels.index(earliest)]
    if not all(label in verified_prefix_set for label in prefix_in_requested):
        return requested_labels, {}, requested_labels[0]

    old_by_label = _blocks_by_label(old_definition)
    is_append_only = earliest not in old_by_label
    if is_append_only:
        # Case A — append-after-success. The earliest invalidated label is a
        # new block that didn't exist in the prior definition, so the verified
        # prefix represents the browser state just before it. Start there.
        return _seed_for_frontier(requested_labels, earliest, verified_outputs, new_definition)

    # Edit-in-place. We lack a browser-anchor signal, so we cannot safely
    # rerun just the edited block (the browser is at post-prefix state, not
    # pre-edit state). Walk back to the nearest upstream state-establishing
    # block within the requested chain. Falls back to the full requested list
    # if no safe upstream anchor can be identified.
    anchor = _nearest_upstream_state_establisher(requested_labels, earliest, new_definition)
    if anchor is None:
        return requested_labels, {}, requested_labels[0]
    return _seed_for_frontier(requested_labels, anchor, verified_outputs, new_definition)


def _seed_for_frontier(
    requested_labels: list[str],
    frontier: str,
    verified_outputs: dict[str, Any],
    new_definition: Any,
) -> tuple[list[str], dict[str, Any], str]:
    try:
        idx = requested_labels.index(frontier)
    except ValueError:
        return requested_labels, {}, requested_labels[0]
    labels_to_execute = requested_labels[idx:]
    prefix_labels = requested_labels[:idx]
    if not prefix_labels:
        return labels_to_execute, {}, frontier
    serialized_configs = _serialized_frontier_block_configs(labels_to_execute, new_definition)
    suffix_refs, block_form_refs, unknown_roots = _classify_frontier_jinja_refs(
        labels_to_execute, new_definition, serialized_configs
    )
    if any(label in block_form_refs for label in prefix_labels):
        # Seeded block_outputs only register <label>_output; block-form refs
        # need a normal upstream execution to populate the <label> namespace.
        return requested_labels, {}, requested_labels[0]
    needed = suffix_refs | block_form_refs
    seed: dict[str, Any] = {}
    for label in prefix_labels:
        if label not in needed:
            continue
        if label not in verified_outputs:
            return requested_labels, {}, requested_labels[0]
        seed[label] = verified_outputs[label]
    if unknown_roots:
        return requested_labels, {}, requested_labels[0]
    return labels_to_execute, seed, frontier


# Watchdog exit reasons. ``success`` means the run reached a trustworthy
# terminal status inside the poll loop OR after the post-drain reconcile.
# The three non-success reasons share the reconcile path but produce distinct
# error messages: ``stagnation`` is the primary trip (no progress signals
# for ``RUN_BLOCKS_STAGNATION_WINDOW_SECONDS`` seconds), ``ceiling`` is the
# last-resort budget-exhausted branch, and ``task_exit_unfinalized`` is the
# rare race where ``execute_workflow`` naturally exits before writing a
# terminal row.
WatchdogExitReason = Literal["success", "stagnation", "ceiling", "per_tool_budget", "task_exit_unfinalized"]


# Block types that legitimately execute long silent periods: one DB write on
# entry, work done without intermediate writes (sleep / LLM call / await human
# input), one write on finish. The watchdog can't distinguish these from
# "stuck", so any invocation that includes one disables stagnation for the
# whole run and relies on the safety ceiling alone.
_QUIET_BLOCK_TYPES: frozenset[str] = frozenset(
    {BlockType.WAIT.value, BlockType.TEXT_PROMPT.value, BlockType.HUMAN_INTERACTION.value}
)


def _any_quiet_block_requested(
    copilot_ctx: CopilotContext,
    labels: list[str] | None,
) -> bool:
    """Return True if any of ``labels`` refers to a block whose type is in
    ``_QUIET_BLOCK_TYPES``. Reuses ``_blocks_by_label`` on the already-loaded
    workflow definition — no DB call.
    """
    if not labels:
        return False
    last_workflow = getattr(copilot_ctx, "last_workflow", None)
    if last_workflow is None:
        return False
    by_label = _blocks_by_label(getattr(last_workflow, "workflow_definition", None))
    for label in labels:
        block = by_label.get(label)
        if block is None:
            continue
        block_type = getattr(block, "block_type", None)
        if block_type is None:
            continue
        block_type_str = block_type.value if hasattr(block_type, "value") else str(block_type)
        if block_type_str in _QUIET_BLOCK_TYPES:
            return True
    return False


async def _read_progress_sources(
    ctx: CopilotContext,
    workflow_run_id: str,
) -> tuple[WorkflowRun | None, datetime | None, datetime | None]:
    """Read one ``workflow_runs`` row + the two progress aggregates needed
    by the watchdog marker. Three cheap indexed queries; no row hydration
    on the aggregate side. The two repo calls run concurrently — they open
    separate async sessions and hit different tables.
    """

    async def _read_timestamps() -> tuple[datetime | None, datetime | None]:
        try:
            return await app.DATABASE.tasks.get_workflow_run_progress_timestamps(
                workflow_run_id=workflow_run_id,
                organization_id=ctx.organization_id,
            )
        except Exception:
            LOG.warning(
                "Workflow run progress timestamps read failed",
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )
            return None, None

    run, (step_ts, block_ts) = await asyncio.gather(
        _safe_read_workflow_run(workflow_run_id, ctx.organization_id, context="watchdog-poll"),
        _read_timestamps(),
    )
    return run, step_ts, block_ts


def _progress_marker(
    run: WorkflowRun | None,
    step_ts: datetime | None,
    block_ts: datetime | None,
) -> tuple[Any, ...]:
    """Hashable scalar snapshot. Changes iff any observable progress has
    occurred at the run, step, or block level since the last poll. Every
    ``update_step`` fires during action execution (including incremental
    token/cost accumulators at ``forge/agent.py:1449``), so
    ``max(step.modified_at)`` is the per-LLM-call heartbeat. Non-task blocks
    (CODE, TEXT_PROMPT) don't create step rows — ``max(workflow_run_block.modified_at)``
    covers that case. ``run.modified_at`` and ``run.status`` catch the
    run-level transitions that happen outside those two tables.
    """
    return (
        run.status if run else None,
        run.modified_at if run is not None else None,
        step_ts,
        block_ts,
    )


async def _watchdog_error_message(
    exit_reason: WatchdogExitReason,
    ctx: AgentContext,
    workflow_run_id: str,
    run: WorkflowRun | None,
    budget_seconds: int,
) -> str:
    """LLM-facing error string for a non-success watchdog exit. No variant uses
    "timed out" or other retry-inviting phrasing — those are SKY-9163 traps.
    """
    if exit_reason == "stagnation":
        body = (
            f"The run has not made progress for {RUN_BLOCKS_STAGNATION_WINDOW_SECONDS}s. "
            f"No step, block, or workflow-run row updates were observed in that window. "
            f"The page is most likely blocked by a captcha, popup, anti-bot challenge, "
            f"hidden validation error, or an infinite-retry loop on an action the agent "
            f"cannot detect is failing."
        )
    elif exit_reason == "per_tool_budget":
        message = (
            f"The run exceeded the {budget_seconds}s per-tool-call budget while still "
            f"making progress. This budget exists so a single in-flight call cannot "
            f"consume the whole copilot session.\n"
            f"Run ID: {workflow_run_id}.\n"
            f"Next step: call get_run_results with this workflow_run_id to inspect what "
            f"the cancelled run actually completed (the in-flight block was cancelled "
            f"mid-execution and may have left partial side effects). Then call "
            f"update_and_run_blocks with a smaller chain — the first 1-2 unverified "
            f"blocks. Verified-prefix state is preserved, so the next call only re-runs "
            f"from the new frontier. Do NOT retry the same chain unchanged — a longer "
            f"run won't fit either."
        )
        current_url, _ = await _fallback_page_info(ctx)
        if current_url:
            message += f" Browser was on: {current_url}"
        return message
    elif exit_reason == "ceiling":
        body = (
            f"The run exceeded the {budget_seconds}s absolute ceiling "
            f"while still showing progress. The workflow is too long to fit in a single "
            f"tool invocation — split it into smaller block groups."
        )
    else:  # task_exit_unfinalized
        last_observed = f"last observed status: {run.status}" if run is not None else "workflow run row was unreadable"
        body = (
            f"The run ended but did not record a trustworthy terminal status in the "
            f"cancellation grace window ({last_observed})."
        )

    message = (
        f"{body} Run ID: {workflow_run_id}. Outcome is uncertain. "
        f"Do NOT re-invoke block-running tools in this session without first calling "
        f"`get_run_results` with this workflow_run_id and reporting the result to the user."
    )
    current_url, _ = await _fallback_page_info(ctx)
    if current_url:
        message += f" Browser was on: {current_url}"
    return message


async def _run_blocks_and_collect_debug(
    params: dict[str, Any],
    ctx: CopilotContext,
    *,
    labels_to_execute: list[str] | None = None,
    block_outputs_to_seed: dict[str, Any] | None = None,
    frontier_start_label: str | None = None,
) -> dict[str, Any]:
    block_labels = params["block_labels"]
    if not block_labels:
        return {"ok": False, "error": "block_labels must not be empty"}

    labels_to_execute = list(labels_to_execute) if labels_to_execute else list(block_labels)
    block_outputs_to_seed = block_outputs_to_seed or {}
    if frontier_start_label is None:
        frontier_start_label = labels_to_execute[0] if labels_to_execute else None

    ctx.last_requested_block_labels = list(block_labels)
    ctx.last_executed_block_labels = list(labels_to_execute)
    ctx.last_frontier_start_label = frontier_start_label

    # Verified state is NOT invalidated pre-run. On a failed / partial run we
    # want the prior verified prefix preserved so the next edit can still use
    # the optimization. YAML-diff-based invalidation for edited/downstream
    # labels happens in update_and_run_blocks_tool at edit time, which is the
    # right moment to drop stale outputs. Full success at the end of this
    # function updates verified state in place (overwriting re-run labels).

    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=ctx.workflow_permanent_id,
        organization_id=ctx.organization_id,
    )
    if not workflow:
        return {"ok": False, "error": f"Workflow not found: {ctx.workflow_permanent_id}"}

    credential_ids = list(
        dict.fromkeys(
            _extract_credential_ids_from_tool_value(params.get("parameters") or {})
            + _extract_credential_ids_from_workflow_definition(workflow.workflow_definition)
        )
    )
    credential_error = await _credential_ids_validation_error(credential_ids, ctx)
    if credential_error is not None:
        return {"ok": False, "error": credential_error}

    for label in block_labels:
        if not workflow.get_output_parameter(label):
            return {"ok": False, "error": f"Block label not found in saved workflow: {label!r}"}

    from skyvern.forge.sdk.schemas.organizations import Organization
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
    from skyvern.services import workflow_service

    org = await app.DATABASE.organizations.get_organization(organization_id=ctx.organization_id)
    if not org:
        return {"ok": False, "error": "Organization not found"}

    organization = Organization.model_validate(org)

    user_params: dict[str, Any] = params.get("parameters") or {}
    all_workflow_params, all_output_params = await asyncio.gather(
        app.WORKFLOW_SERVICE.get_workflow_parameters(workflow_id=workflow.workflow_id),
        app.DATABASE.workflow_params.get_workflow_output_parameters(workflow_id=workflow.workflow_id),
    )

    # Short-circuit before a wasted workflow execution when the definition
    # JSON has drifted from the persisted parameter rows that runtime reads.
    invariant_error = _parameter_binding_invariant_error(workflow, all_workflow_params, all_output_params)
    if invariant_error is not None:
        summary, missing_persisted, missing_from_definition = invariant_error
        return {
            "ok": False,
            "error": summary,
            "data": {
                "workflow_run_id": None,
                "overall_status": "failed",
                "failure_reason": summary,
                "requested_block_labels": list(block_labels),
                "executed_block_labels": [],
                "frontier_start_label": None,
                "blocks": [],
                "failure_categories": [
                    {
                        "category": "PARAMETER_BINDING_ERROR",
                        "confidence_float": 0.99,
                        "reasoning": "Pre-run invariant: workflow_definition and persisted parameter rows disagree",
                    }
                ],
                "missing_persisted": missing_persisted,
                "missing_from_definition": missing_from_definition,
            },
        }

    data: dict[str, Any] = {}
    for wp in all_workflow_params:
        if wp.key in user_params:
            data[wp.key] = user_params[wp.key]
        elif wp.default_value is not None:
            data[wp.key] = wp.default_value
        else:
            placeholder = _placeholder_for_parameter_type(wp.workflow_parameter_type)
            if placeholder is not None:
                data[wp.key] = placeholder
                LOG.info(
                    "Auto-filled missing workflow parameter for copilot test run",
                    parameter_key=wp.key,
                    parameter_type=str(wp.workflow_parameter_type),
                )

    # Without a session, the workflow service launches the browser in-process,
    # which only works in worker pods (cloakbrowser isn't in the API image).
    session_err = await ensure_browser_session(ctx)
    if session_err is not None:
        return session_err

    workflow_request = WorkflowRequestBody(
        data=data if data else None,
        browser_session_id=ctx.browser_session_id,
        # Copilot test runs don't need scrolling post-action screenshots;
        # the ForgeAgent's split screenshots (used for LLM context) are unaffected.
        max_screenshot_scrolls=0,
    )

    workflow_run = await workflow_service.prepare_workflow(
        workflow_id=ctx.workflow_permanent_id,
        organization=organization,
        workflow_request=workflow_request,
        template=False,
        version=None,
        max_steps=None,
        request_id=None,
        copilot_session_id=ctx.workflow_copilot_chat_id,
    )

    from skyvern.utils.files import initialize_skyvern_state_file

    await initialize_skyvern_state_file(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=ctx.organization_id,
    )

    run_task = asyncio.create_task(
        app.WORKFLOW_SERVICE.execute_workflow(
            workflow_run_id=workflow_run.workflow_run_id,
            api_key="copilot-agent",
            organization=organization,
            browser_session_id=ctx.browser_session_id,
            block_labels=labels_to_execute,
            block_outputs=block_outputs_to_seed or None,
        )
    )

    # The OpenAI Agents SDK wraps this tool in
    # ``asyncio.wait_for(..., timeout=RUN_BLOCKS_SAFETY_CEILING_SECONDS)``, so
    # the inner budget leaves 10 s of headroom for the cancel-drain and
    # post-drain reconcile to finish before the SDK's own cancel fires.
    #
    # Do NOT short-circuit on client disconnect: the agent loop runs to
    # completion after the SSE stream is gone so its reply persists
    # (SKY-8986); aborting mid-block would strand the run without debug
    # output for the final chat message.
    initial_run, initial_step_ts, initial_block_ts = await _read_progress_sources(ctx, workflow_run.workflow_run_id)
    progress_marker = _progress_marker(initial_run, initial_step_ts, initial_block_ts)
    last_progress_monotonic = time.monotonic()
    started_monotonic = last_progress_monotonic
    final_status: str | None = None
    run: Any = initial_run
    exit_reason: WatchdogExitReason | None = None
    run_cancelled_by_watchdog = False
    # Quiet blocks (WAIT/TEXT_PROMPT/HUMAN_INTERACTION) legitimately have
    # DB-silent periods; disable stagnation for any invocation that includes
    # one. Safety ceiling still applies.
    stagnation_enabled = not _any_quiet_block_requested(ctx, labels_to_execute)
    # Active block runs use the tighter per-tool budget so a single in-flight
    # call cannot consume the whole copilot session. Quiet-block runs keep the
    # long safety ceiling because HumanInteractionBlock can legitimately pause
    # indefinitely.
    budget_exit_reason: WatchdogExitReason
    if stagnation_enabled:
        budget_seconds = _active_block_run_budget_seconds(ctx)
        budget_exit_reason = "per_tool_budget"
    else:
        budget_seconds = max(1, RUN_BLOCKS_SAFETY_CEILING_SECONDS - 10)
        budget_exit_reason = "ceiling"

    # Mid-tool narrator bridge: feed block-status changes and step-level
    # heartbeats into NarratorState so the narration ticker keeps emitting
    # while a long workflow run is in flight.
    narrator_state: NarratorState | None = getattr(ctx, "narrator_state", None)
    narrator_enabled = narrator_state is not None and narration_handler_available()
    seen_block_states: dict[str, str] = {}
    prior_block_ts: datetime | None = initial_block_ts
    prior_step_ts: datetime | None = initial_step_ts
    last_block_fetch_monotonic = 0.0

    try:
        while True:
            await asyncio.sleep(RUN_BLOCKS_POLL_INTERVAL_SECONDS)

            run, step_ts, block_ts = await _read_progress_sources(ctx, workflow_run.workflow_run_id)

            if narrator_enabled:
                assert narrator_state is not None  # narrator_enabled implies non-None
                tick_result = await narrator_poll_tick(
                    narrator_state,
                    current_block_ts=block_ts,
                    current_step_ts=step_ts,
                    prior_block_ts=prior_block_ts,
                    prior_step_ts=prior_step_ts,
                    last_block_fetch_monotonic=last_block_fetch_monotonic,
                    seen_block_states=seen_block_states,
                    fetch_block_statuses=lambda: app.DATABASE.observer.get_workflow_run_blocks(
                        workflow_run_id=workflow_run.workflow_run_id,
                        organization_id=ctx.organization_id,
                    ),
                    stream=ctx.stream,
                )
                prior_block_ts = tick_result.prior_block_ts
                prior_step_ts = tick_result.prior_step_ts
                last_block_fetch_monotonic = tick_result.last_block_fetch_monotonic

            if run and WorkflowRunStatus(run.status).is_final():
                final_status = run.status
                exit_reason = "success"
                break

            if run_task.done():
                # Row not terminal yet — shared reconcile path below flips
                # most of these back to success after post-drain reread.
                exit_reason = "task_exit_unfinalized"
                break

            now = time.monotonic()
            new_marker = _progress_marker(run, step_ts, block_ts)
            # A run in ``paused`` status (e.g. HumanInteractionBlock) is a
            # user-driven wait, not stagnation — never trip.
            is_paused = run is not None and run.status == WorkflowRunStatus.paused.value
            stagnation_active = stagnation_enabled and not is_paused

            if new_marker != progress_marker:
                progress_marker = new_marker
                last_progress_monotonic = now
            elif stagnation_active and now - last_progress_monotonic >= RUN_BLOCKS_STAGNATION_WINDOW_SECONDS:
                exit_reason = "stagnation"
                break

            if now - started_monotonic >= budget_seconds:
                exit_reason = budget_exit_reason
                break

        if exit_reason is not None and exit_reason != "success":
            # Pre-cancel read first: a legitimate self-finalize (user/block
            # cancel, or any terminal the run wrote itself) can land between
            # the last poll and here, and trusting it avoids the
            # synthetic-``canceled`` ambiguity that the post-drain reread
            # has to exclude. Then cancel + reread +
            # ``_trusted_post_drain_status`` applies SKY-9167's success-race
            # recovery uniformly to all three non-success exit reasons.
            pre_cancel_run = await _safe_read_workflow_run(
                workflow_run.workflow_run_id, ctx.organization_id, context="pre-cancel"
            )
            if pre_cancel_run is not None and WorkflowRunStatus(pre_cancel_run.status).is_final():
                final_status = pre_cancel_run.status
                run = pre_cancel_run
                exit_reason = "success"
            else:
                await _cancel_run_task_if_not_final(run_task, workflow_run.workflow_run_id)
                run_cancelled_by_watchdog = True
                run = await _safe_read_workflow_run(
                    workflow_run.workflow_run_id, ctx.organization_id, context="post-drain"
                )
                trusted = _trusted_post_drain_status(run)
                if trusted is not None:
                    final_status = trusted
                    exit_reason = "success"

        if exit_reason != "success":
            assert exit_reason is not None  # narrows for mypy; outer check excludes "success" but not None
            ctx.pending_reconciliation_run_id = workflow_run.workflow_run_id
            error_msg = await _watchdog_error_message(
                exit_reason, ctx, workflow_run.workflow_run_id, run, budget_seconds
            )
            result: dict[str, Any] = {"ok": False, "error": error_msg}
            if exit_reason == "per_tool_budget":
                # Stable failure_categories entry so consecutive budget trips
                # hash to the same streak signature; without it the run_id in
                # ``error_msg`` would make every trip unique.
                result["data"] = {
                    "workflow_run_id": workflow_run.workflow_run_id,
                    "overall_status": run.status if run is not None else None,
                    "failure_reason": error_msg,
                    "failure_categories": [
                        {
                            "category": PER_TOOL_BUDGET_FAILURE_CATEGORY,
                            "confidence_float": 1.0,
                            "reasoning": (
                                f"Per-tool-call budget of {budget_seconds}s exceeded; "
                                "the run was making progress but cannot fit in a single tool call."
                            ),
                        }
                    ],
                }
            if run_cancelled_by_watchdog:
                result[_INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY] = True
            return result
    except asyncio.CancelledError:
        # The SDK's @function_tool(timeout=...) cancelled us mid-poll. Shield
        # the cleanup so the parent cancellation can't interrupt it mid-await.
        # If the shield itself is cancelled, fall back to a detached task
        # that outlives tool teardown and still reconciles workflow state.
        try:
            await asyncio.shield(_cancel_run_task_if_not_final(run_task, workflow_run.workflow_run_id))
        except asyncio.CancelledError:
            fallback = asyncio.ensure_future(_cancel_run_task_if_not_final(run_task, workflow_run.workflow_run_id))
            _DETACHED_CLEANUP_TASKS.add(fallback)
            fallback.add_done_callback(_DETACHED_CLEANUP_TASKS.discard)
            fallback.add_done_callback(_log_detached_cleanup_failure)
        raise
    finally:
        # Belt and braces. If any exit path above missed a cancel — e.g. an
        # unexpected exception bubbling out of the poll loop — make sure the
        # run_task is at least signaled to cancel so we don't leak it.
        if not run_task.done():
            run_task.cancel()

    if run and run.browser_session_id:
        ctx.browser_session_id = run.browser_session_id

    blocks = await app.DATABASE.observer.get_workflow_run_blocks(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=ctx.organization_id,
    )

    results = []
    block_outputs_by_label: dict[str, Any] = {}
    for block in blocks:
        block_result: dict[str, Any] = {
            "label": block.label,
            "block_type": block.block_type.name if hasattr(block.block_type, "name") else str(block.block_type),
            "status": block.status,
        }
        if block.failure_reason:
            block_result["failure_reason"] = block.failure_reason
        if hasattr(block, "output") and block.output:
            block_result["extracted_data"] = block.output
            if block.label is not None:
                block_outputs_by_label[block.label] = block.output
        results.append(block_result)

    await _attach_action_traces(blocks, results, ctx.organization_id)

    # final_status is guaranteed set here: every non-success exit returns
    # above, and the success path always populates final_status.
    assert final_status is not None
    run_ok = WorkflowRunStatus(final_status) == WorkflowRunStatus.completed

    action_trace_summary: list[str] = []
    first_failed = next(
        (r for r in results if r.get("status") in _FAILED_BLOCK_STATUSES and r.get("action_trace")),
        None,
    )
    if first_failed is not None:
        action_trace_summary = _summarize_action_trace(first_failed.get("action_trace"))

    # Compute the action-sequence fingerprint BEFORE we strip action_trace.
    # Stash it on a pending ctx field so update_repeated_failure_state can
    # compare the NEW fingerprint against ctx.last_action_sequence_fingerprint
    # (the PRIOR value) and increment the streak. Never enters the LLM-visible
    # packet. Drives the repeated-action streak that hard-aborts a stuck
    # fill→click→re-fill loop in _tool_loop_error.
    ctx.pending_action_sequence_fingerprint = compute_action_sequence_fingerprint(results)

    # Per-block action_trace is for derivation only — keep it out of the
    # compact packet. get_run_results remains the heavier inspection path.
    for entry in results:
        entry.pop("action_trace", None)

    current_url, page_title = await _fallback_page_info(ctx)

    screenshot_b64: str | None = None
    if not run_ok and ctx.browser_session_id:
        try:
            browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                session_id=ctx.browser_session_id,
                organization_id=ctx.organization_id,
            )
            if browser_state:
                page = await browser_state.get_or_create_page()
                if SettingsManager.get_settings().BROWSER_CURSOR_VISUALIZATION:
                    try:
                        await SkyvernFrame.hide_cursor_overlay(page)
                    except Exception:
                        pass
                try:
                    screenshot_bytes = await page.screenshot(type="png")
                finally:
                    if SettingsManager.get_settings().BROWSER_CURSOR_VISUALIZATION:
                        try:
                            await SkyvernFrame.show_cursor_overlay(page)
                        except Exception:
                            pass
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        except Exception:
            LOG.debug("Failed to capture post-run screenshot", exc_info=True)

    result_data: dict[str, Any] = {
        "workflow_run_id": workflow_run.workflow_run_id,
        "browser_session_id": ctx.browser_session_id,
        "overall_status": final_status,
        "requested_block_labels": list(block_labels),
        "executed_block_labels": list(labels_to_execute),
        "frontier_start_label": frontier_start_label,
        "blocks": results,
        "current_url": current_url,
        "page_title": page_title,
        "action_trace_summary": action_trace_summary,
    }
    if screenshot_b64 is not None:
        result_data["screenshot_base64"] = screenshot_b64
    if not run_ok and run and getattr(run, "failure_reason", None):
        result_data["failure_reason"] = run.failure_reason

    # Update verified prefix state ONLY on a fully-successful run. A failed
    # suffix run leaves the browser in post-failure state, so we must not
    # trust blocks that individually succeeded inside it.
    if run_ok and all(r.get("status") == "completed" for r in results):
        for label, output in block_outputs_by_label.items():
            ctx.verified_block_outputs[label] = output
        existing_prefix = list(getattr(ctx, "verified_prefix_labels", []) or [])
        existing_set = set(existing_prefix)
        for label in labels_to_execute:
            if label not in existing_set:
                existing_prefix.append(label)
                existing_set.add(label)
        ctx.verified_prefix_labels = existing_prefix

    return build_run_blocks_response(run_ok, result_data)


async def _get_run_results(params: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
    workflow_run_id = params.get("workflow_run_id")

    if not workflow_run_id:
        # Include every final state so the agent can inspect failures via the
        # fallback. Non-final states (created/queued/running/paused) remain
        # excluded — reading block records from an in-flight run is unsafe.
        runs = await app.WORKFLOW_SERVICE.get_workflow_runs_for_workflow_permanent_id(
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
            page=1,
            page_size=1,
            status=[
                WorkflowRunStatus.completed,
                WorkflowRunStatus.failed,
                WorkflowRunStatus.terminated,
                WorkflowRunStatus.canceled,
                WorkflowRunStatus.timed_out,
            ],
        )
        if not runs:
            return {"ok": False, "error": "No runs found for this workflow."}
        workflow_run_id = runs[0].workflow_run_id

    run = await app.DATABASE.workflow_runs.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=ctx.organization_id,
    )
    if not run:
        return {"ok": False, "error": f"Workflow run not found: {workflow_run_id}"}

    blocks = await app.DATABASE.observer.get_workflow_run_blocks(
        workflow_run_id=workflow_run_id,
        organization_id=ctx.organization_id,
    )

    results = []
    for block in blocks:
        block_result: dict[str, Any] = {
            "label": block.label,
            "block_type": block.block_type.name if hasattr(block.block_type, "name") else str(block.block_type),
            "status": block.status,
        }
        if block.failure_reason:
            block_result["failure_reason"] = block.failure_reason
        output = truncate_output(getattr(block, "output", None))
        if output:
            block_result["output"] = output
        results.append(block_result)

    await _attach_action_traces(blocks, results, ctx.organization_id)
    await _attach_failed_block_screenshots(blocks, results, ctx.organization_id)

    result_data: dict[str, Any] = {
        "workflow_run_id": workflow_run_id,
        "overall_status": run.status,
        "blocks": results,
    }
    if getattr(run, "failure_reason", None):
        result_data["failure_reason"] = run.failure_reason

    return {
        "ok": True,
        "data": result_data,
    }


async def _fallback_page_info(ctx: AgentContext) -> tuple[str, str]:
    if not ctx.browser_session_id:
        return "", ""
    try:
        from skyvern.cli.core.session_manager import get_page

        page, _ = await get_page(session_id=ctx.browser_session_id)
        if page:
            return page.url, await page.title()
    except Exception:
        pass
    return "", ""


async def _resolve_url_title(raw: dict[str, Any], ctx: AgentContext) -> tuple[str, str]:
    """Extract URL and title from raw MCP result, falling back to live page info."""
    browser_ctx = raw.get("browser_context", {})
    url = browser_ctx.get("url", "")
    title = browser_ctx.get("title", "")
    if not url:
        url, fallback_title = await _fallback_page_info(ctx)
        if fallback_title:
            title = fallback_title
    return url, title


# Block types the copilot must never emit. They delegate the entire goal to
# a separate agent, which bypasses copilot-level block decomposition and
# obfuscates issues the copilot should surface/handle directly.
_COPILOT_BANNED_BLOCK_TYPES: frozenset[str] = frozenset({"task", "task_v2"})

# Shared suffix across every LLM-facing rejection message for banned
# block emission — the pre-hook (schema-lookup reject) and the post-
# emission detector both steer the LLM toward the same alternatives.
_COPILOT_BANNED_BLOCK_ALTERNATIVES = (
    "Use `navigation` for page actions (filling forms, clicking, multi-step flows), "
    "`extraction` for data extraction, `validation` for completion checks, "
    "`login` for authentication, or `goto_url` for pure URL navigation."
)

_GOTO_URL_DYNAMIC_STATE_KEY_TOKENS = frozenset(
    {
        "attr",
        "attribute",
        "category",
        "constraint",
        "criteria",
        "criterion",
        "facet",
        "filter",
        "option",
        "refine",
        "refinement",
        "selected",
        "selection",
        "toggle",
    }
)
_GOTO_URL_DYNAMIC_STATE_VALUE_TOKENS = frozenset(
    {
        "facet",
        "filter",
        "refine",
        "refinement",
        "selected",
        "selection",
    }
)
_GOTO_URL_VERIFICATION_BLOCK_TYPES = frozenset({"code", "navigation", "validation"})
_GOTO_URL_DATA_PRODUCING_BLOCK_TYPES = frozenset({"extraction", "text_prompt"})
_GOTO_URL_STATE_ACTION_TOKENS = frozenset(
    {
        "apply",
        "check",
        "choose",
        "click",
        "confirm",
        "confirmed",
        "ensure",
        "inspect",
        "observe",
        "select",
        "set",
        "verify",
        "verified",
        "verifies",
        "verifying",
    }
)
_GOTO_URL_STATE_EVIDENCE_TOKENS = frozenset(
    {
        "active",
        "aria",
        "checked",
        "checkbox",
        "chip",
        "constraint",
        "control",
        "criteria",
        "criterion",
        "dom",
        "facet",
        "filter",
        "form",
        "input",
        "option",
        "selected",
        "state",
        "url",
        "value",
    }
)
_GOTO_URL_CODE_EVIDENCE_TOKENS = frozenset(
    {
        "all_text_contents",
        "attribute",
        "contents",
        "evaluate",
        "inner_text",
        "locator",
        "query",
        "query_selector",
        "selector",
        "text",
        "text_content",
    }
)
_GOTO_URL_DISMISSAL_TOKENS = frozenset({"banner", "cookie", "cookies", "dismiss", "modal", "popup", "popups"})
_GOTO_URL_SEARCH_SETUP_ACTION_TOKENS = frozenset(
    {
        "choose",
        "enter",
        "fill",
        "input",
        "pick",
        "select",
        "set",
        "type",
    }
)
_GOTO_URL_SEARCH_SETUP_CONTEXT_TOKENS = frozenset(
    {
        "calendar",
        "checkout",
        "checkin",
        "date",
        "dates",
        "find",
        "lookup",
        "query",
        "search",
    }
)
_GOTO_URL_SEARCH_SETUP_TERMINAL_TOKENS = frozenset(
    {
        "active",
        "checked",
        "checkbox",
        "chip",
        "constraint",
        "control",
        "dom",
        "extract",
        "extraction",
        "facet",
        "filter",
        "result",
        "results",
        "verified",
        "verify",
    }
)
_GOTO_URL_STRUCTURAL_STATE_SPLIT_RE = re.compile(r"[;|]")
_GOTO_URL_STRUCTURAL_ASSIGNMENT_LHS_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_GOTO_URL_VALUE_SCHEME_RE = re.compile(r"^\s*[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def _banned_block_reject_message(items: list[tuple[str, str]]) -> str:
    """Uniform error text for the post-emission reject, sharing the
    alternatives suffix with the schema pre-hook."""
    labels = ", ".join(sorted({label for label, _ in items}))
    types = sorted({block_type for _, block_type in items})
    types_part = " / ".join(repr(t) for t in types)
    return (
        f"Block type {types_part} is not available in the workflow copilot. "
        f"Offending labels: [{labels}]. "
        f"{_COPILOT_BANNED_BLOCK_ALTERNATIVES}"
    )


def _record_banned_block_reject_span(source_tool: str, items: list[tuple[str, str]]) -> None:
    """Emit the dedicated ``update_workflow_banned_block_reject`` span used
    by post-rollout logfire trend queries."""
    with copilot_span(
        "update_workflow_banned_block_reject",
        data={
            "labels": [label for label, _ in items],
            "block_types": sorted({block_type for _, block_type in items}),
            "source_tool": source_tool,
        },
    ):
        pass


def _proxy_location_trace_value(proxy_location: Any) -> Any:
    if proxy_location is None:
        return None
    if hasattr(proxy_location, "value"):
        return proxy_location.value
    if hasattr(proxy_location, "model_dump"):
        return proxy_location.model_dump(mode="json")
    return proxy_location


def _raw_yaml_proxy_location(workflow_yaml: str) -> tuple[bool, Any]:
    try:
        parsed_yaml = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return False, None

    if not isinstance(parsed_yaml, dict) or "proxy_location" not in parsed_yaml:
        return False, None
    return True, _proxy_location_trace_value(parsed_yaml.get("proxy_location"))


def _record_workflow_proxy_location_span(workflow_yaml: str, workflow: Workflow) -> None:
    input_present, input_proxy_location = _raw_yaml_proxy_location(workflow_yaml)
    effective_proxy_location = _proxy_location_trace_value(workflow.proxy_location)
    with copilot_span(
        "workflow_proxy_location_normalized",
        data={
            "input_proxy_location_present": input_present,
            "input_proxy_location": input_proxy_location,
            "effective_proxy_location": effective_proxy_location,
        },
    ):
        pass


def _record_goto_url_state_shortcut_reject_span(source_tool: str, items: list[dict[str, Any]]) -> None:
    with copilot_span(
        "goto_url_state_shortcut_reject",
        data={
            "source_tool": source_tool,
            "items": items,
            "labels": [item.get("label") for item in items],
            "dynamic_state_keys": sorted({key for item in items for key in item.get("dynamic_state_keys", [])}),
        },
    ):
        pass


def _record_goto_url_stable_search_drip_reject_span(source_tool: str, items: list[dict[str, Any]]) -> None:
    with copilot_span(
        "goto_url_stable_search_drip_reject",
        data={
            "source_tool": source_tool,
            "items": items,
            "entry_labels": [item.get("entry_label") for item in items],
            "navigation_labels": [item.get("navigation_labels", []) for item in items],
        },
    ):
        pass


def _record_navigation_url_stable_search_reject_span(source_tool: str, items: list[dict[str, Any]]) -> None:
    with copilot_span(
        "navigation_url_stable_search_reject",
        data={
            "source_tool": source_tool,
            "items": items,
            "labels": [item.get("label") for item in items],
        },
    ):
        pass


def _collect_banned_block_items(blocks: list[Any]) -> list[tuple[str, str]]:
    """Recursively walk ``blocks`` (mirroring
    :func:`skyvern.forge.sdk.copilot.block_goal_wrapping._wrap_blocks_in_place`)
    and return ``(label, normalized_block_type)`` for every block whose type is
    in :data:`_COPILOT_BANNED_BLOCK_TYPES`. Blocks missing ``label`` are
    skipped — the downstream Pydantic validator surfaces those errors on its
    own."""
    items: list[tuple[str, str]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        raw_type = block.get("block_type")
        if isinstance(raw_type, str):
            normalized = raw_type.strip().lower()
            if normalized in _COPILOT_BANNED_BLOCK_TYPES:
                label = block.get("label")
                if isinstance(label, str):
                    items.append((label, normalized))
        loop_blocks = block.get("loop_blocks")
        if isinstance(loop_blocks, list):
            items.extend(_collect_banned_block_items(loop_blocks))
    return items


def _parse_workflow_blocks(yaml_str: str | None) -> list[Any] | None:
    """Parse ``yaml_str`` and return ``workflow_definition.blocks`` as a list,
    or ``None`` if the YAML is missing, unparseable, or not in the expected
    shape. Graceful on every failure so callers can treat ``None`` as 'nothing
    to compare against.'"""
    if not yaml_str:
        return None
    try:
        parsed = safe_load_no_dates(yaml_str)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return None
    blocks = definition.get("blocks")
    return blocks if isinstance(blocks, list) else None


def _normalized_query_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _query_token_parts(value: str) -> set[str]:
    parts: set[str] = set()
    for part in value.split("_"):
        if not part:
            continue
        parts.add(part)
        if len(part) > 3 and part.endswith("s"):
            parts.add(part[:-1])
    return parts


def _is_dynamic_state_query_key(key: str) -> bool:
    return bool(_query_token_parts(key) & _GOTO_URL_DYNAMIC_STATE_KEY_TOKENS)


def _is_dynamic_state_query_value(value: str) -> bool:
    return bool(_query_token_parts(value) & _GOTO_URL_DYNAMIC_STATE_VALUE_TOKENS)


def _is_url_like_query_value(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if stripped.startswith("/") or _GOTO_URL_VALUE_SCHEME_RE.match(stripped):
        return True
    first_equals = stripped.find("=")
    first_query = stripped.find("?")
    return first_query != -1 and (first_equals == -1 or first_query < first_equals)


def _assignment_lhs(clause: str) -> str | None:
    if "=" not in clause:
        return None
    lhs, rhs = clause.split("=", 1)
    lhs = lhs.strip()
    if not lhs or not rhs.strip():
        return None
    if not _GOTO_URL_STRUCTURAL_ASSIGNMENT_LHS_RE.fullmatch(lhs):
        return None
    return _normalized_query_token(lhs)


def _is_structural_dynamic_state_query_value(value: str, outer_key: str) -> bool:
    if not value or _is_url_like_query_value(value):
        return False

    clauses = [clause.strip() for clause in _GOTO_URL_STRUCTURAL_STATE_SPLIT_RE.split(value) if clause.strip()]
    if not clauses:
        return False

    assignment_lhses = [lhs for lhs in (_assignment_lhs(clause) for clause in clauses) if lhs is not None]
    if len(assignment_lhses) >= 2:
        return True
    if len(assignment_lhses) == 1 and assignment_lhses[0] != outer_key:
        return True
    return False


def _dynamic_state_query_keys(url: Any) -> list[str]:
    if not isinstance(url, str) or not url:
        return []
    try:
        pairs = parse_qsl(urlparse(url).query, keep_blank_values=True)
    except ValueError:
        return []
    if not pairs:
        return []

    normalized_pairs = [(_normalized_query_token(key), _normalized_query_token(value), value) for key, value in pairs]
    key_counts = Counter(key for key, _, _ in normalized_pairs if key)
    dynamic_keys: set[str] = set()
    for key, value, raw_value in normalized_pairs:
        if not key:
            continue
        if _is_structural_dynamic_state_query_value(raw_value, key):
            dynamic_keys.add(key)
        elif _is_dynamic_state_query_key(key):
            dynamic_keys.add(key)
        elif key_counts[key] > 1:
            dynamic_keys.add(key)
        elif _is_dynamic_state_query_value(value):
            dynamic_keys.add(key)
    return sorted(dynamic_keys)


def _block_label_from_yaml(block: dict[str, Any]) -> str | None:
    label = block.get("label")
    return label if isinstance(label, str) and label else None


def _next_top_level_block(
    *,
    blocks: list[dict[str, Any]],
    by_label: dict[str, dict[str, Any]],
    index_by_label: dict[str, int],
    current: dict[str, Any],
) -> dict[str, Any] | None:
    next_label = current.get("next_block_label")
    if isinstance(next_label, str) and next_label:
        return by_label.get(next_label)

    label = _block_label_from_yaml(current)
    if label is None:
        return None
    index = index_by_label.get(label)
    if index is None:
        return None
    if index + 1 >= len(blocks):
        return None
    return blocks[index + 1]


def _next_data_block_without_verification(
    *,
    blocks: list[dict[str, Any]],
    by_label: dict[str, dict[str, Any]],
    index_by_label: dict[str, int],
    start: dict[str, Any],
) -> dict[str, Any] | None:
    seen: set[str] = set()
    current = start
    while True:
        next_block = _next_top_level_block(
            blocks=blocks,
            by_label=by_label,
            index_by_label=index_by_label,
            current=current,
        )
        if next_block is None:
            return None
        current = next_block

        label = _block_label_from_yaml(current)
        if label is not None:
            if label in seen:
                return None
            seen.add(label)

        block_type = str(current.get("block_type", "")).strip().lower()
        if _block_verifies_dynamic_goto_url_state(current):
            return None
        if block_type in _GOTO_URL_DATA_PRODUCING_BLOCK_TYPES:
            return current


def _block_state_text(block: dict[str, Any]) -> str:
    values = []
    for key in (
        "navigation_goal",
        "complete_criterion",
        "terminate_criterion",
        "validation_goal",
        "code",
        "description",
        "title",
    ):
        value = block.get(key)
        if isinstance(value, str):
            values.append(value)
    return _normalized_query_token(" ".join(values))


def _block_primary_goal_text(block: dict[str, Any]) -> str:
    for key in ("navigation_goal", "validation_goal", "code", "description", "title"):
        value = block.get(key)
        if not isinstance(value, str) or not value:
            continue
        first_fence = value.find("```")
        if first_fence == -1:
            return _normalized_query_token(value)
        second_fence = value.find("```", first_fence + 3)
        if second_fence != -1:
            fenced = value[first_fence + 3 : second_fence].strip()
            if fenced:
                return _normalized_query_token(fenced)
        return _normalized_query_token(value)
    return ""


def _block_verifies_dynamic_goto_url_state(block: dict[str, Any]) -> bool:
    block_type = str(block.get("block_type", "")).strip().lower()
    if block_type not in _GOTO_URL_VERIFICATION_BLOCK_TYPES:
        return False

    parts = _query_token_parts(_block_state_text(block))
    if not parts:
        return False

    has_state_evidence = bool(parts & _GOTO_URL_STATE_EVIDENCE_TOKENS)
    if block_type == "code":
        return has_state_evidence and bool(parts & _GOTO_URL_CODE_EVIDENCE_TOKENS)
    if block_type == "validation":
        return has_state_evidence
    if parts & _GOTO_URL_DISMISSAL_TOKENS and not parts & {"active", "checked", "selected"}:
        return False
    return has_state_evidence and bool(parts & _GOTO_URL_STATE_ACTION_TOKENS)


def _is_stable_search_setup_navigation(block: dict[str, Any]) -> bool:
    if str(block.get("block_type", "")).strip().lower() != "navigation":
        return False

    parts = _query_token_parts(_block_primary_goal_text(block))
    if not parts:
        return False
    if parts & _GOTO_URL_SEARCH_SETUP_TERMINAL_TOKENS:
        return False
    return bool(parts & _GOTO_URL_SEARCH_SETUP_ACTION_TOKENS) and bool(parts & _GOTO_URL_SEARCH_SETUP_CONTEXT_TOKENS)


def _detect_navigation_url_stable_search_setups(workflow_yaml: str | None) -> list[dict[str, Any]]:
    blocks = _parse_workflow_blocks(workflow_yaml)
    if not blocks:
        return []

    findings: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if not isinstance(block.get("url"), str) or not block["url"].strip():
            continue
        if not _is_stable_search_setup_navigation(block):
            continue
        findings.append(
            {
                "label": _block_label_from_yaml(block) or "<missing label>",
                "url": block["url"],
            }
        )
    return findings


def _detect_click_driven_stable_search_drips(workflow_yaml: str | None) -> list[dict[str, Any]]:
    blocks = _parse_workflow_blocks(workflow_yaml)
    if not blocks:
        return []

    top_level_blocks = [block for block in blocks if isinstance(block, dict)]
    findings: list[dict[str, Any]] = []
    entry_label: str | None = None
    stable_nav_labels: list[str] = []

    for block in top_level_blocks:
        block_type = str(block.get("block_type", "")).strip().lower()
        label = _block_label_from_yaml(block) or "<missing label>"

        if block_type == "goto_url":
            entry_label = label
            stable_nav_labels = []
            continue

        if entry_label is None:
            continue

        if block_type in {"code", "validation"}:
            stable_nav_labels = []
            continue

        if block_type in _GOTO_URL_DATA_PRODUCING_BLOCK_TYPES:
            entry_label = None
            stable_nav_labels = []
            continue

        if _is_stable_search_setup_navigation(block):
            stable_nav_labels.append(label)
            if len(stable_nav_labels) >= 2:
                findings.append(
                    {
                        "entry_label": entry_label,
                        "navigation_labels": stable_nav_labels.copy(),
                    }
                )
                entry_label = None
                stable_nav_labels = []
            continue

    return findings


def _detect_unverified_goto_url_state_shortcuts(workflow_yaml: str | None) -> list[dict[str, Any]]:
    blocks = _parse_workflow_blocks(workflow_yaml)
    if not blocks:
        return []

    top_level_blocks = [block for block in blocks if isinstance(block, dict)]
    by_label: dict[str, dict[str, Any]] = {}
    index_by_label: dict[str, int] = {}
    for index, block in enumerate(top_level_blocks):
        label = _block_label_from_yaml(block)
        if label is None:
            continue
        by_label[label] = block
        index_by_label[label] = index

    findings: list[dict[str, Any]] = []
    for block in top_level_blocks:
        if str(block.get("block_type", "")).strip().lower() != "goto_url":
            continue
        dynamic_state_keys = _dynamic_state_query_keys(block.get("url"))
        if not dynamic_state_keys:
            continue
        next_data_block = _next_data_block_without_verification(
            blocks=top_level_blocks,
            by_label=by_label,
            index_by_label=index_by_label,
            start=block,
        )
        if next_data_block is None:
            continue
        findings.append(
            {
                "label": _block_label_from_yaml(block) or "<missing label>",
                "dynamic_state_keys": dynamic_state_keys,
                "next_data_block_label": _block_label_from_yaml(next_data_block) or "<missing label>",
            }
        )
    return findings


_GOTO_URL_STATE_SHORTCUT_MESSAGE_LIMIT = 5


def _goto_url_state_shortcut_message(items: list[dict[str, Any]]) -> str:
    details = []
    for item in items[:_GOTO_URL_STATE_SHORTCUT_MESSAGE_LIMIT]:
        label = item.get("label", "?")
        keys = ", ".join(str(key) for key in item.get("dynamic_state_keys", []))
        next_label = item.get("next_data_block_label", "?")
        details.append(f"{label}: query keys [{keys}] feed directly into {next_label}")
    if len(items) > _GOTO_URL_STATE_SHORTCUT_MESSAGE_LIMIT:
        details.append(f"(and {len(items) - _GOTO_URL_STATE_SHORTCUT_MESSAGE_LIMIT} more)")
    return (
        "Workflow validation failed: goto_url may bootstrap stable page state, but URL-encoded dynamic page "
        "state must be verified from the live page before data extraction. Add a code, validation, or navigation block "
        "between the URL shortcut and extraction that inspects active chips, checked controls, search fields, "
        "or DOM/link/form evidence; applies any missing state through UI actions when needed; and verifies all "
        "requested constraints are active together. Offending shortcuts: "
        f"{'; '.join(details)}"
    )


def _goto_url_stable_search_drip_message(items: list[dict[str, Any]]) -> str:
    details = []
    for item in items[:_GOTO_URL_STATE_SHORTCUT_MESSAGE_LIMIT]:
        entry_label = item.get("entry_label", "?")
        labels = ", ".join(str(label) for label in item.get("navigation_labels", []))
        details.append(f"{entry_label}: click-driven stable search setup blocks [{labels}]")
    if len(items) > _GOTO_URL_STATE_SHORTCUT_MESSAGE_LIMIT:
        details.append(f"(and {len(items) - _GOTO_URL_STATE_SHORTCUT_MESSAGE_LIMIT} more)")
    return (
        "Workflow validation failed: after a goto_url entry page, do not build stateful search setup as one "
        "click-driven navigation block per stable field. Use observed same-site evidence from the live page "
        "(current URL, form action, field names/values, result links, or URL deltas) to bootstrap stable search "
        "state with goto_url, or insert a short code/validation block that inspects the DOM and verifies/navigates "
        "the stable results page. Reserve click-driven navigation for missing dynamic state or when URL/form "
        f"evidence is insufficient. Offending sequence: {'; '.join(details)}"
    )


def _navigation_url_stable_search_message(items: list[dict[str, Any]]) -> str:
    details = []
    for item in items[:_GOTO_URL_STATE_SHORTCUT_MESSAGE_LIMIT]:
        details.append(f"{item.get('label', '?')}: navigation url={item.get('url', '?')}")
    if len(items) > _GOTO_URL_STATE_SHORTCUT_MESSAGE_LIMIT:
        details.append(f"(and {len(items) - _GOTO_URL_STATE_SHORTCUT_MESSAGE_LIMIT} more)")
    return (
        "Workflow validation failed: a navigation block with a URL is combining entry-page navigation with "
        "stable search setup. Split pure page loading into a goto_url block first. Then use observed same-site "
        "evidence (current URL, form action, field names/values, result links, or URL deltas) to bootstrap "
        "stable search state with goto_url, or add a short code/validation block to inspect the DOM before "
        f"click-driven setup. Offending blocks: {'; '.join(details)}"
    )


def _detect_new_banned_blocks(
    submitted_yaml: str,
    prior_workflow_yaml: str | None,
) -> list[tuple[str, str]]:
    """Return ``[(label, block_type), ...]`` for every banned-type block in
    ``submitted_yaml`` whose label is NOT present as a banned-type block in
    ``prior_workflow_yaml``. Pure: no I/O, no logging.

    Recurses into ``for_loop.loop_blocks`` mirroring
    :func:`skyvern.forge.sdk.copilot.block_goal_wrapping._wrap_blocks_in_place`.
    Legacy workflows that carry ``task`` / ``task_v2`` blocks under unchanged
    labels produce an empty list and therefore do not reject.

    Malformed YAML, missing ``workflow_definition``, or a non-list ``blocks``
    all produce an empty list — the downstream Pydantic validation in
    ``_process_workflow_yaml`` surfaces the specific parse / shape error on
    its own path.
    """
    submitted_blocks = _parse_workflow_blocks(submitted_yaml)
    if submitted_blocks is None:
        return []
    submitted_items = _collect_banned_block_items(submitted_blocks)
    if not submitted_items:
        return []
    prior_blocks = _parse_workflow_blocks(prior_workflow_yaml)
    prior_labels = {label for label, _ in _collect_banned_block_items(prior_blocks or [])}
    return [(label, block_type) for label, block_type in submitted_items if label not in prior_labels]


_CHALLENGE_WAIT_PATTERN = re.compile(
    r"\b(anti[-_\s]?bot|bot[-_\s]?block|captcha|challenge|cloudflare|ip[-_\s]?block|waf)\b",
    re.IGNORECASE,
)


def _has_confirmed_waf_or_site_block(ctx: Any) -> bool:
    if getattr(ctx, "last_test_anti_bot", None):
        return True
    return _get_int_attr(ctx, "probable_site_block_streak_count") >= PROBABLE_SITE_BLOCK_STREAK_STOP_AT


def _get_int_attr(ctx: Any, name: str, default: int = 0) -> int:
    value = getattr(ctx, name, default)
    return value if isinstance(value, int) else default


def _block_challenge_wait_text(block: dict[str, Any]) -> str:
    values = []
    for key in ("label", "title", "description", "navigation_goal", "complete_criterion"):
        value = block.get(key)
        if isinstance(value, str):
            values.append(value)
    return " ".join(values)


def _detect_timing_only_challenge_wait_blocks(submitted_yaml: str | None) -> list[str]:
    submitted_blocks = _parse_workflow_blocks(submitted_yaml)
    if submitted_blocks is None:
        return []
    labels: list[str] = []
    for block in _iter_yaml_blocks(submitted_blocks):
        raw_type = block.get("block_type")
        if not isinstance(raw_type, str) or raw_type.strip().lower() != "wait":
            continue
        label = block.get("label")
        if not isinstance(label, str):
            continue
        if _CHALLENGE_WAIT_PATTERN.search(_block_challenge_wait_text(block)):
            labels.append(label)
    return labels


def _timing_only_challenge_wait_reject_message(ctx: Any, submitted_yaml: str | None) -> str | None:
    if not _has_confirmed_waf_or_site_block(ctx):
        return None
    labels = _detect_timing_only_challenge_wait_blocks(submitted_yaml)
    if not labels:
        return None
    labels_text = ", ".join(sorted(set(labels)))
    return (
        "Workflow validation failed: timing-only challenge wait blocks are not allowed after confirmed "
        "anti-bot/WAF or repeated site-block evidence. "
        f"Offending labels: [{labels_text}]. "
        "Do not add wait/delay-only blocks for this blocker; use a conditional challenge check that takes a "
        "real action, try a materially different proxy/source if allowed, or stop and explain the blocker."
    )


async def _get_block_schema_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    """Short-circuit requests for banned block types with an explicit error.
    Without this pre-hook the underlying MCP tool silently redirects ``task``
    and ``task_v2`` queries to ``navigation``'s schema, which makes the LLM
    think the banned types are available."""
    block_type = params.get("block_type")
    if not isinstance(block_type, str):
        return None
    normalized = block_type.strip().lower()
    if normalized not in _COPILOT_BANNED_BLOCK_TYPES:
        return None
    return {
        "ok": False,
        "error": f"Block type {block_type!r} is not available in the workflow copilot. {_COPILOT_BANNED_BLOCK_ALTERNATIVES}",
    }


async def _get_block_schema_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    """Scrub banned block types from list-mode responses. Belt-and-suspenders
    against future drift in ``BLOCK_SUMMARIES`` (which currently omits them)."""
    data = result.get("data")
    if isinstance(data, dict):
        block_types = data.get("block_types")
        if isinstance(block_types, dict):
            for banned in _COPILOT_BANNED_BLOCK_TYPES:
                block_types.pop(banned, None)
    return result


async def _evaluate_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    expr = params.get("expression", "").lower()
    if ".click()" in expr or ".click(" in expr:
        return {
            "ok": False,
            "error": "Do not use evaluate to click elements. Use the 'click' tool with a CSS selector instead.",
        }
    return None


_JQUERY_SELECTOR_RE = re.compile(
    r":(?:contains|eq|first|last|gt|lt|nth|has|visible|hidden|checked)\s*\(", re.IGNORECASE
)


async def _click_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    selector = params.get("selector", "")
    if not selector:
        return None
    if _JQUERY_SELECTOR_RE.search(selector):
        return {
            "ok": False,
            "error": (
                f"Invalid selector: {selector!r}. "
                "jQuery pseudo-selectors like :contains(), :eq(), :first, :visible are NOT valid CSS. "
                "Use standard CSS selectors instead. Examples: "
                "nth-of-type() instead of :eq(), "
                "[data-attr] or tag.class for filtering, "
                "or use the 'evaluate' tool with JS: "
                "document.querySelectorAll('button').forEach("
                "b => {{ if (b.textContent.includes('Download')) b.click() }})"
            ),
        }
    return None


async def _navigate_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok"):
        data = result.pop("data", {})
        result["url"] = data.get("url", "")
        result["next_step"] = (
            "Page loaded. You MUST now use evaluate, "
            "get_browser_screenshot, or click to inspect page content "
            "before responding."
        )
    return result


async def _screenshot_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url, title = await _resolve_url_title(raw, ctx)
        result["data"] = {
            "screenshot_base64": data.get("data", ""),
            "url": url,
            "title": title,
        }
    return result


async def _click_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url, title = await _resolve_url_title(raw, ctx)
        result["data"] = {
            "selector": data.get("selector", ""),
            "url": url,
            "title": title,
        }
    return result


async def _type_text_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url, _ = await _resolve_url_title(raw, ctx)
        result["data"] = {
            "selector": data.get("selector", ""),
            "typed_length": data.get("text_length", 0),
            "url": url,
        }
    return result


async def _evaluate_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        result["data"].pop("sdk_equivalent", None)
        if "url" not in result["data"]:
            url, _ = await _resolve_url_title(raw, ctx)
            if url:
                result["data"]["url"] = url
    return result


async def _scroll_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url, _ = await _resolve_url_title(raw, ctx)
        result["data"] = {
            "direction": data.get("direction", ""),
            "amount": data.get("pixels") or data.get("amount"),
            "url": url,
        }
    return result


async def _select_option_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url, _ = await _resolve_url_title(raw, ctx)
        result["data"] = {
            "selector": data.get("selector", ""),
            "value": data.get("value", ""),
            "url": url,
        }
    return result


async def _press_key_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url, _ = await _resolve_url_title(raw, ctx)
        result["data"] = {
            "key": data.get("key", ""),
            "selector": data.get("selector", ""),
            "url": url,
        }
    return result


def get_skyvern_mcp_alias_map() -> dict[str, str]:
    return {
        "get_block_schema": "skyvern_block_schema",
        "validate_block": "skyvern_block_validate",
        "navigate_browser": "skyvern_navigate",
        "get_browser_screenshot": "skyvern_screenshot",
        "evaluate": "skyvern_evaluate",
        "click": "skyvern_click",
        "type_text": "skyvern_type",
        "scroll": "skyvern_scroll",
        "console_messages": "skyvern_console_messages",
        "select_option": "skyvern_select_option",
        "press_key": "skyvern_press_key",
    }


def _build_skyvern_mcp_overlays() -> dict[str, SchemaOverlay]:
    return {
        "get_block_schema": SchemaOverlay(
            pre_hook=_get_block_schema_pre_hook,
            post_hook=_get_block_schema_post_hook,
        ),
        "validate_block": SchemaOverlay(),
        "navigate_browser": SchemaOverlay(
            description=(
                "Navigate the debug browser to a URL. "
                "Use this to reset browser state or navigate to a starting page before running blocks."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            requires_browser=True,
            post_hook=_navigate_post_hook,
        ),
        "get_browser_screenshot": SchemaOverlay(
            description=(
                "Take a screenshot of the current debug browser session. "
                "Returns a base64-encoded PNG image. "
                "Use this to see what the browser looks like after running blocks."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "selector"}),
            forced_args={"inline": True},
            requires_browser=True,
            post_hook=_screenshot_post_hook,
        ),
        "evaluate": SchemaOverlay(
            description=(
                "Execute JavaScript in the browser and return the result. "
                "Use this to inspect DOM state, read values, or run arbitrary JS."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            requires_browser=True,
            timeout=30,
            pre_hook=_evaluate_pre_hook,
            post_hook=_evaluate_post_hook,
        ),
        "click": SchemaOverlay(
            description=(
                "Click an element in the browser. Use a CSS selector, an AI intent "
                "description, or both for resilient targeting. "
                "IMPORTANT: jQuery pseudo-selectors like :contains(), :eq(), :first, "
                ":visible are NOT valid CSS. Use standard selectors: "
                "'button.download', 'a[href*=\"pdf\"]', '#submit-btn', "
                "'table tr:nth-of-type(2) td a'."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "button", "click_count"}),
            requires_browser=True,
            timeout=15,
            pre_hook=_click_pre_hook,
            post_hook=_click_post_hook,
        ),
        "type_text": SchemaOverlay(
            description=(
                "Type text into an input element. Use a CSS selector, an AI intent "
                "description, or both to target the field. "
                "Optionally clear the field first. Use this for form filling. "
                "NEVER type inline passwords, API keys, tokens, cookies, TOTP/OTP "
                "codes, private keys, or other raw credentials/secrets received in "
                "chat — stop and follow the CREDENTIAL HANDLING refusal rule in the "
                "system prompt instead."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "delay"}),
            required_overrides=["text"],
            arg_transforms={"clear_first": "clear"},
            requires_browser=True,
            timeout=15,
            post_hook=_type_text_post_hook,
        ),
        "scroll": SchemaOverlay(
            description=(
                "Scroll the page in a direction (up/down/left/right) by pixel amount, "
                "or scroll a specific element into view using intent or selector. "
                "Use this to reveal content below the fold."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            requires_browser=True,
            post_hook=_scroll_post_hook,
        ),
        "console_messages": SchemaOverlay(
            description=(
                "Read console log messages from the browser. "
                "Use level='error' to find JavaScript errors. "
                "This is a read-only diagnostic tool."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            requires_browser=True,
        ),
        "select_option": SchemaOverlay(
            description=(
                "Select an option from a <select> dropdown. Provide the value to select "
                "and use selector or intent to target the element. "
                "For free-text inputs, use type_text instead."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "timeout"}),
            required_overrides=["value"],
            requires_browser=True,
            timeout=15,
            post_hook=_select_option_post_hook,
        ),
        "press_key": SchemaOverlay(
            description=(
                "Press a keyboard key (Enter, Tab, Escape, ArrowDown, etc.). "
                "Optionally focus an element first via selector or intent. "
                "Use for form submission, tab navigation, or closing dialogs."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            required_overrides=["key"],
            requires_browser=True,
            post_hook=_press_key_post_hook,
        ),
    }


def _record_workflow_update_result(copilot_ctx: Any, result: dict[str, Any]) -> None:
    if not (result.get("ok") and "_workflow" in result):
        return

    wf = result["_workflow"]
    copilot_ctx.last_workflow = wf
    _clear_resolved_per_tool_budget_problem_labels(copilot_ctx, wf)
    copilot_ctx.last_workflow_yaml = copilot_ctx.workflow_yaml or None
    copilot_ctx.effective_workflow_proxy_location = getattr(wf, "proxy_location", None) or ProxyLocation.RESIDENTIAL
    data = result.get("data")
    if isinstance(data, dict):
        block_count = data.get("block_count")
        if isinstance(block_count, int):
            copilot_ctx.last_update_block_count = block_count
    copilot_ctx.last_test_ok = None
    copilot_ctx.last_test_failure_reason = None
    # A fresh workflow edit invalidates the prior test's failure state —
    # otherwise an exhausted POST_UPDATE_NUDGE on the new draft would raise
    # CopilotNonRetriableNavError with the old run's error, telling the user
    # to "verify the URL" for a URL they just corrected in the new draft.
    copilot_ctx.last_test_non_retriable_nav_error = None
    copilot_ctx.non_retriable_nav_error_last_emitted_signature = None
    copilot_ctx.workflow_persisted = True

    # Block-running failures keyed off (labels, parameters) go stale once the
    # workflow itself changes — without this clear, a user who fixes the bug
    # via update_workflow gets a LOOP DETECTED on the next legitimate run.
    clear_failed_step_tracker_for_tools_in_ctx(copilot_ctx, BLOCK_RUNNING_TOOLS)


def _pre_run_workflow_coverage_error(copilot_ctx: Any) -> str | None:
    block_count = getattr(copilot_ctx, "last_update_block_count", None)
    if not isinstance(block_count, int):
        return None

    user_message = getattr(copilot_ctx, "user_message", "")
    request_policy = getattr(copilot_ctx, "request_policy", None)
    completion_contract = getattr(request_policy, "completion_contract", None)
    if isinstance(completion_contract, str):
        completion_contract = completion_contract.strip() or None
    else:
        completion_contract = None

    if not _goal_likely_needs_more_blocks(user_message, block_count, completion_contract):
        return None

    nudge_count = getattr(copilot_ctx, "coverage_nudge_count", 0)
    if nudge_count >= 1:
        return None
    copilot_ctx.coverage_nudge_count = nudge_count + 1
    return (
        f"{POST_INTERMEDIATE_SUCCESS_NUDGE} The workflow was saved with {block_count} block"
        f"{'' if block_count == 1 else 's'}, but it has not been run because the request-policy "
        "completion contract still leaves distinct requested actions uncovered."
    )


def _analyze_run_blocks(result: dict[str, Any]) -> tuple[str | None, bool, list[dict] | None]:
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
            if isinstance(cat, dict) and cat.get("category") == "ANTI_BOT_DETECTION":
                anti_bot_match = cat.get("reasoning", "anti-bot pattern detected")
                break
        return anti_bot_match, False, precomputed_categories

    # Collect texts for scanning and data-block stats in one pass
    texts_to_scan: list[str] = []
    html = data.get("visible_elements_html")
    if isinstance(html, str):
        texts_to_scan.append(html)

    has_data_blocks = False
    any_data_output = False

    blocks = data.get("blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            reason = block.get("failure_reason")
            if isinstance(reason, str):
                texts_to_scan.append(reason)
            block_type = block.get("block_type")
            if block_type in _DATA_PRODUCING_BLOCK_TYPES and block.get("status") == "completed":
                has_data_blocks = True
                payload = _block_data_payload(block.get("extracted_data"), block_type)
                if _is_meaningful_extracted_data(payload):
                    any_data_output = True

    combined = "\n".join(texts_to_scan)
    categories = classify_from_failure_reason(combined)
    if categories:
        for cat in categories:
            if cat.get("category") == "ANTI_BOT_DETECTION":
                anti_bot_match = cat.get("reasoning", "anti-bot pattern detected")
                break

    empty_data_blocks = has_data_blocks and not any_data_output
    return anti_bot_match, empty_data_blocks, categories


# Generic failure-reason template emitted by the shared agent when the
# browser-side scraper catches ScrapingFailed / NoElementFound. Matching on
# the template (not the shared classifier) lets the copilot notice a repeated
# site-block/unreadable-page pattern even though the classifier routes it to
# DATA_EXTRACTION_FAILURE, not ANTI_BOT_DETECTION.
# Coupling note: these substrings come from the run-level failure_reason
# produced when the shared scraper raises ScrapingFailed. If the template
# wording changes, update this tuple and the test that locks it in
# (tests/unit/test_copilot_probable_site_block.py).
_PROBABLE_SITE_BLOCK_FAILURE_REASON_SUBSTRINGS = (
    "failed to load the website",
    "page may have navigated unexpectedly",
)


def _detect_probable_site_block_wall(result: dict[str, Any]) -> bool:
    """True when a block failed with the site-load template and the failure is
    not a non-retriable nav error (DNS / SSL / invalid URL are owned by
    :func:`_detect_non_retriable_nav_error`)."""
    if bool(result.get("ok", False)):
        return False
    if _detect_non_retriable_nav_error(result):
        return False
    data = result.get("data")
    if not isinstance(data, dict):
        return False
    blocks = data.get("blocks")
    if not isinstance(blocks, list):
        return False

    for block in blocks:
        if not isinstance(block, dict):
            continue
        reason = block.get("failure_reason")
        if isinstance(reason, str):
            lowered = reason.lower()
            if any(sub in lowered for sub in _PROBABLE_SITE_BLOCK_FAILURE_REASON_SUBSTRINGS):
                return True
    return False


def _detect_non_retriable_nav_error(result: dict[str, Any]) -> str | None:
    """Return the first failure_reason that matches SKIP_INNER_NAV_RETRY_ERRORS
    (DNS / cert / SSL / invalid URL), preferring run-level over block-level.
    Same set is_skip_inner_retry_error uses at the browser layer, so the copilot
    classifies on exactly the patterns that already short-circuit retries in
    navigate_with_retry (skyvern/webeye/navigation.py)."""
    return next((reason for reason in iter_failure_reasons(result) if is_skip_inner_retry_error(reason)), None)


def _record_run_blocks_result(copilot_ctx: Any, result: dict[str, Any]) -> None:
    run_ok = bool(result.get("ok", False))
    # Watchdog cancels normally count as ok=False; only a coincident total
    # timeout softens to ``None`` to keep the unvalidated WIP rescue open.
    cancelled_by_watchdog = result.get(_INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY) is True
    timeout_latched = bool(copilot_ctx.copilot_total_timeout_exceeded)
    copilot_ctx.last_test_ok = None if (cancelled_by_watchdog and timeout_latched) else run_ok
    copilot_ctx.last_test_failure_reason = None
    copilot_ctx.last_test_suspicious_success = False
    copilot_ctx.suspicious_success_nudge_count = 0
    copilot_ctx.last_test_anti_bot = None
    prior_budget_flag = copilot_ctx.last_failure_category_top == PER_TOOL_BUDGET_FAILURE_CATEGORY
    copilot_ctx.last_failure_category_top = None
    copilot_ctx.last_test_non_retriable_nav_error = None

    anti_bot_match, empty_data_blocks, failure_categories = _analyze_run_blocks(result)
    if anti_bot_match:
        copilot_ctx.last_test_anti_bot = anti_bot_match
    if failure_categories:
        top = failure_categories[0]
        if isinstance(top, dict):
            top_category = top.get("category")
            if isinstance(top_category, str):
                copilot_ctx.last_failure_category_top = top_category

    # A fresh budget trip on a different chain should get the dedicated split
    # nudge again rather than falling through to the generic failed-test path,
    # so reset the cap when the latest run is not itself a budget trip.
    if prior_budget_flag and copilot_ctx.last_failure_category_top != PER_TOOL_BUDGET_FAILURE_CATEGORY:
        copilot_ctx.per_tool_budget_nudge_count = 0

    # Expose full failure classification in tool output for agent reasoning
    if failure_categories:
        data = result.get("data")
        if isinstance(data, dict):
            data["failure_categories"] = failure_categories

    if run_ok:
        if empty_data_blocks:
            copilot_ctx.last_test_ok = None
            copilot_ctx.last_test_suspicious_success = True
            copilot_ctx.null_data_streak_count = getattr(copilot_ctx, "null_data_streak_count", 0) + 1
            copilot_ctx.last_test_failure_reason = (
                "All blocks completed but data-producing blocks "
                "(extraction/text_prompt) produced no meaningful output "
                "(missing, empty, or all-null fields). "
                "The workflow may not be working correctly."
            )
            # Clean-ish success (no scrape-fail pattern): reset the streak.
            copilot_ctx.probable_site_block_streak_count = 0
            update_repeated_failure_state(copilot_ctx, result)
            return
        copilot_ctx.failed_test_nudge_count = 0
        copilot_ctx.null_data_streak_count = 0
        copilot_ctx.probable_site_block_streak_count = 0
        copilot_ctx.last_failed_workflow_yaml = None
        # Real success: clear the signature latch so a subsequent bad URL in
        # the same session can re-fire the stop nudge.
        copilot_ctx.non_retriable_nav_error_last_emitted_signature = None
        copilot_ctx.last_good_workflow = copilot_ctx.last_workflow
        copilot_ctx.last_good_workflow_yaml = copilot_ctx.last_workflow_yaml
        update_repeated_failure_state(copilot_ctx, result)
        return

    copilot_ctx.last_failed_workflow_yaml = getattr(copilot_ctx, "workflow_yaml", None)
    copilot_ctx.last_test_non_retriable_nav_error = _detect_non_retriable_nav_error(result)
    if _detect_probable_site_block_wall(result):
        copilot_ctx.probable_site_block_streak_count += 1
    else:
        copilot_ctx.probable_site_block_streak_count = 0

    data = result.get("data")
    if isinstance(data, dict):
        blocks = data.get("blocks")
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("failure_reason"):
                    copilot_ctx.last_test_failure_reason = str(block["failure_reason"])
                    break
    if result.get("error") and copilot_ctx.last_test_failure_reason is None:
        copilot_ctx.last_test_failure_reason = str(result["error"])
    update_repeated_failure_state(copilot_ctx, result)


@function_tool(
    name_override="update_workflow",
    tool_input_guardrails=[_WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL],
)
async def update_workflow_tool(
    ctx: RunContextWrapper,
    workflow_yaml: str,
) -> str:
    """Validate and update the workflow YAML definition.
    Provide the complete workflow YAML as a string.
    Returns the validated workflow or validation errors.

    If a `goto_url` URL encodes dynamic page state, do not wire it
    directly to extraction. Add a `code`, `validation`, or `navigation` block
    first that verifies the live page state from active chips, checked controls,
    fields, or DOM/link/form evidence.

    After a `goto_url` entry page for a stateful search, do not add one
    click-driven `navigation` block per stable field (query terms, dates,
    search-form inputs). Use observed current URL / form action / field
    names / result links / URL deltas to bootstrap stable search state with
    `goto_url`, or add a short `code`/`validation` block to inspect the DOM.
    Do not bypass this by combining a URL and stable search setup in a
    `navigation` block; split pure loading into `goto_url` first.
    """
    copilot_ctx = ctx.context
    arguments = {"workflow_yaml": workflow_yaml}
    loop_error = _tool_loop_error(copilot_ctx, "update_workflow", arguments)
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})

    with copilot_span("update_workflow", data={"yaml_length": len(workflow_yaml)}):
        result = await _update_workflow(
            arguments,
            copilot_ctx,
            allow_missing_credentials=getattr(copilot_ctx, "allow_untested_workflow_draft", False) is True,
        )
        _record_workflow_update_result(copilot_ctx, result)
        record_tool_step_result_for_ctx(copilot_ctx, "update_workflow", arguments, result)
    sanitized = sanitize_tool_result_for_llm("update_workflow", result)
    return json.dumps(sanitized)


@function_tool(name_override="list_credentials")
async def list_credentials_tool(
    ctx: RunContextWrapper,
    page: int = 1,
    page_size: int = 10,
) -> str:
    """List stored credentials (metadata only — never passwords or secrets).
    Use this to find credential IDs for login blocks.

    Paginated. `page_size` caps at 50. The response includes `has_more`;
    before concluding no credential exists, keep incrementing `page` until
    `has_more` is `false` — otherwise you risk telling the user to create
    a credential they have already stored on a later page.
    """
    copilot_ctx = ctx.context
    arguments = {"page": page, "page_size": page_size}
    loop_error = _tool_loop_error(copilot_ctx, "list_credentials", arguments)
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})

    result = await _list_credentials(arguments, copilot_ctx)
    record_tool_step_result_for_ctx(copilot_ctx, "list_credentials", arguments, result)
    sanitized = sanitize_tool_result_for_llm("list_credentials", result)
    return json.dumps(sanitized)


async def _frontier_plan_for_current_workflow(
    ctx: AgentContext, block_labels: list[str]
) -> tuple[list[str], dict[str, Any], str | None]:
    """Plan execution for the plain (no YAML update) path."""
    prior_definition = await _get_prior_workflow_definition(ctx)
    return _plan_frontier(ctx, block_labels, prior_definition, prior_definition)


def _run_blocks_span_data(
    block_labels: list[str],
    labels_to_execute: list[str],
    frontier_start_label: str | None,
    seeded_outputs: dict[str, Any],
    ctx: Any,
) -> dict[str, Any]:
    return {
        "requested_block_labels": block_labels,
        "executed_block_labels": labels_to_execute,
        "frontier_start_label": frontier_start_label,
        "seeded_output_count": len(seeded_outputs or {}),
        "repeated_failure_streak_count": int(getattr(ctx, "repeated_failure_streak_count", 0) or 0),
        "block_count": len(block_labels),
    }


@function_tool(
    name_override="run_blocks_and_collect_debug",
    timeout=RUN_BLOCKS_SAFETY_CEILING_SECONDS,
    strict_mode=False,
)
async def run_blocks_tool(
    ctx: RunContextWrapper,
    block_labels: list[str],
    parameters: dict[str, Any] | None = None,
) -> Any:
    """Run one or more blocks of the current workflow, wait for completion,
    and return compact debug output (status, failure reason, visible elements).
    The workflow must be saved before running blocks.
    Block labels must match labels in the saved workflow.

    For a diagnostic / observational complaint about the current workflow,
    this tool is not the first response. Follow the system prompt's
    inspect-and-clarify path first: inspect current workflow context and
    existing run evidence before deciding whether a fresh run is needed.

    Pass runtime values for workflow parameters via the `parameters` dict —
    keys must match the workflow parameter `key` field. When the user has
    supplied concrete non-secret values in their message (names, emails, IDs),
    pass them on the first call rather than letting the workflow fall back to
    placeholders. For sensitive values (password, secret, token, api_key,
    credential, totp, otp, one_time_code, private_key, auth) — call
    `list_credentials` and use a credential parameter whose default_value is
    the stored `credential_id`. If no stored credential matches, do NOT pass
    the inline value via `parameters`; stop and follow the CREDENTIAL
    HANDLING refusal rule in the system prompt.

    When a prior `goto_url` used URL params for dynamic page state, the
    blocks you run must include live-page verification before extraction. If
    the visible state is uncertain or only partially applied, inspect it
    through a `code` or `validation` block, or apply missing state through a
    `navigation` block, instead of retrying guessed URL params.
    """
    copilot_ctx = ctx.context
    arguments = {"block_labels": block_labels, "parameters": parameters or {}}
    policy_error = _request_policy_tool_error(copilot_ctx, "run_blocks_and_collect_debug")
    if policy_error:
        return json.dumps({"ok": False, "error": policy_error})

    loop_error = _tool_loop_error(copilot_ctx, "run_blocks_and_collect_debug", arguments)
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})

    labels_to_execute, block_outputs_to_seed, frontier_start_label = await _frontier_plan_for_current_workflow(
        copilot_ctx, block_labels
    )

    with copilot_span(
        "run_blocks",
        data=_run_blocks_span_data(
            block_labels,
            labels_to_execute,
            frontier_start_label,
            block_outputs_to_seed,
            copilot_ctx,
        ),
    ):
        result = await _run_blocks_and_collect_debug(
            arguments,
            copilot_ctx,
            labels_to_execute=labels_to_execute,
            block_outputs_to_seed=block_outputs_to_seed,
            frontier_start_label=frontier_start_label,
        )
        _record_run_blocks_result(copilot_ctx, result)
        record_tool_step_result_for_ctx(copilot_ctx, "run_blocks_and_collect_debug", arguments, result)
        enqueue_screenshot_from_result(copilot_ctx, result)

    sanitized = sanitize_tool_result_for_llm("run_blocks_and_collect_debug", result)
    return json.dumps(sanitized)


@function_tool(name_override="get_run_results")
async def get_run_results_tool(
    ctx: RunContextWrapper,
    workflow_run_id: str | None = None,
) -> str:
    """Fetch results from a previous workflow run.
    Returns block statuses, failure reasons, and output data.
    If workflow_run_id is omitted, fetches the most recently created finished
    run (completed, failed, canceled, terminated, or timed_out — excludes
    in-flight runs). For unambiguous results in concurrent-run scenarios,
    pass an explicit workflow_run_id from a prior tool response.
    """
    copilot_ctx = ctx.context
    params: dict[str, Any] = {}
    if workflow_run_id:
        params["workflow_run_id"] = workflow_run_id
    loop_error = _tool_loop_error(copilot_ctx, "get_run_results", params)
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})

    result = await _get_run_results(params, copilot_ctx)
    _record_per_tool_budget_problem_blocks_from_results(copilot_ctx, result)
    _maybe_clear_reconciliation_flag(copilot_ctx, result)
    record_tool_step_result_for_ctx(copilot_ctx, "get_run_results", params, result)

    sanitized = sanitize_tool_result_for_llm("get_run_results", result)
    return json.dumps(sanitized)


@function_tool(
    name_override="update_and_run_blocks",
    timeout=RUN_BLOCKS_SAFETY_CEILING_SECONDS,
    strict_mode=False,
    tool_input_guardrails=[_WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL],
)
async def update_and_run_blocks_tool(
    ctx: RunContextWrapper,
    workflow_yaml: str,
    block_labels: list[str],
    parameters: dict[str, Any] | None = None,
) -> Any:
    """Update the workflow YAML and immediately run the specified blocks in one step.
    Use this instead of calling update_workflow and run_blocks_and_collect_debug separately.
    The workflow must validate successfully before blocks are run.

    For a diagnostic / observational complaint about the current workflow,
    this tool is not the first response. Follow the system prompt's
    inspect-and-clarify path first, and only update/run when the user asked
    for an edit or the inspected evidence makes the correction clear.

    Pass runtime values for workflow parameters via the `parameters` dict —
    keys must match the workflow parameter `key` field. When the user has
    supplied concrete non-secret values in their message (names, emails, IDs),
    pass them on the first call rather than letting the workflow fall back to
    placeholders. For sensitive values (password, secret, token, api_key,
    credential, totp, otp, one_time_code, private_key, auth) — call
    `list_credentials` and use a credential parameter whose default_value is
    the stored `credential_id`. If no stored credential matches, do NOT pass
    the inline value via `parameters`; stop and follow the CREDENTIAL
    HANDLING refusal rule in the system prompt.

    Treat `goto_url` as a bootstrap for stateful search/result pages. If URL query
    params encode dynamic page state, include a `code`, `validation`, or
    `navigation` block before extraction that verifies every requested
    constraint is active simultaneously from live page state, and only refine
    URL params when they are grounded in observed DOM/link/form state or
    observed URL deltas.

    After a `goto_url` entry page for a stateful search, do not add one
    click-driven `navigation` block per stable field (query terms, dates,
    search-form inputs). Use observed current URL / form action / field
    names / result links / URL deltas to bootstrap stable search state with
    `goto_url`, or add a short `code`/`validation` block to inspect the DOM.
    Do not bypass this by combining a URL and stable search setup in a
    `navigation` block; split pure loading into `goto_url` first.
    """
    copilot_ctx = ctx.context
    arguments = {"workflow_yaml": workflow_yaml, "block_labels": block_labels, "parameters": parameters or {}}
    policy_error = _request_policy_tool_error(copilot_ctx, "update_and_run_blocks")
    if policy_error:
        return json.dumps({"ok": False, "error": policy_error})

    loop_error = _tool_loop_error(copilot_ctx, "update_and_run_blocks", arguments)
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})

    # Snapshot the prior workflow definition BEFORE _update_workflow saves
    # the new one — we need the pre-update state to diff against.
    prior_definition = await _get_prior_workflow_definition(copilot_ctx)

    # Wrap each block's navigation_goal / complete_criterion / terminate_criterion
    # with the effective "big goal" context so downstream LLMs (verifier,
    # validation-block prompt) have user-intent framing — mirrors TaskV2.
    block_goal_main_goal = copilot_ctx.block_goal_main_goal or copilot_ctx.user_message
    if block_goal_main_goal:
        workflow_yaml = wrap_block_goals(workflow_yaml, block_goal_main_goal)
    else:
        LOG.warning("update_and_run_blocks invoked without block-goal context; skipping block-goal wrap")

    # Step 1: Update the workflow
    with copilot_span("update_workflow", data={"yaml_length": len(workflow_yaml)}):
        update_result = await _update_workflow(
            {"workflow_yaml": workflow_yaml},
            copilot_ctx,
            allow_missing_credentials=False,
        )
        _record_workflow_update_result(copilot_ctx, update_result)

    if not update_result.get("ok"):
        record_tool_step_result_for_ctx(copilot_ctx, "update_and_run_blocks", arguments, update_result)
        sanitized = sanitize_tool_result_for_llm("update_workflow", update_result)
        return json.dumps(sanitized)

    coverage_error = _pre_run_workflow_coverage_error(copilot_ctx)
    if coverage_error:
        result = {
            "ok": False,
            "error": coverage_error,
            "data": {
                "block_count": copilot_ctx.last_update_block_count,
                "workflow_updated": True,
                "workflow_run_skipped": True,
            },
        }
        record_tool_step_result_for_ctx(copilot_ctx, "update_and_run_blocks", arguments, result)
        return json.dumps(result)

    # Step 2: Compute frontier and run the blocks
    new_definition = None
    if copilot_ctx.last_workflow is not None:
        new_definition = getattr(copilot_ctx.last_workflow, "workflow_definition", None)

    # YAML-diff-based invalidation: drop any verified-prefix entries whose
    # block config changed (or are downstream of a change) BEFORE the next
    # run plans the frontier. This is the only point where we shrink verified
    # state — failed runs leave it intact so subsequent edits can still use
    # the optimization. On append-only diffs this is a no-op.
    if prior_definition is not None and new_definition is not None and copilot_ctx.verified_prefix_labels:
        try:
            edit_invalidated = _find_invalidated_labels(
                prior_definition, new_definition, list(copilot_ctx.verified_prefix_labels)
            )
        except Exception:
            edit_invalidated = set()
        if edit_invalidated:
            for label in edit_invalidated:
                copilot_ctx.verified_block_outputs.pop(label, None)
            copilot_ctx.verified_prefix_labels = [
                label for label in copilot_ctx.verified_prefix_labels if label not in edit_invalidated
            ]

    labels_to_execute, block_outputs_to_seed, frontier_start_label = _plan_frontier(
        copilot_ctx, block_labels, prior_definition, new_definition
    )

    with copilot_span(
        "run_blocks",
        data=_run_blocks_span_data(
            block_labels,
            labels_to_execute,
            frontier_start_label,
            block_outputs_to_seed,
            copilot_ctx,
        ),
    ):
        run_result = await _run_blocks_and_collect_debug(
            {"block_labels": block_labels, "parameters": parameters or {}},
            copilot_ctx,
            labels_to_execute=labels_to_execute,
            block_outputs_to_seed=block_outputs_to_seed,
            frontier_start_label=frontier_start_label,
        )
        _record_run_blocks_result(copilot_ctx, run_result)
        record_tool_step_result_for_ctx(copilot_ctx, "update_and_run_blocks", arguments, run_result)
        enqueue_screenshot_from_result(copilot_ctx, run_result)

    sanitized = sanitize_tool_result_for_llm("run_blocks_and_collect_debug", run_result)
    return json.dumps(sanitized)


NATIVE_TOOLS = [
    update_workflow_tool,
    list_credentials_tool,
    run_blocks_tool,
    get_run_results_tool,
    update_and_run_blocks_tool,
]

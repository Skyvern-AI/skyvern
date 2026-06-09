"""Copilot agent tools — native handlers, hooks, and registration."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urljoin, urlparse

import structlog
import yaml
from agents import ToolGuardrailFunctionOutput, ToolInputGuardrail, ToolInputGuardrailData, function_tool
from opentelemetry import trace as otel_trace

try:
    from bs4 import BeautifulSoup  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — bs4 is a transitive dep but discovery degrades gracefully without it.
    BeautifulSoup = None  # type: ignore[assignment, misc]
from agents.run_context import RunContextWrapper
from jinja2.sandbox import SandboxedEnvironment
from pydantic import AliasChoices, BaseModel, Field, ValidationError

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot.attribution import resolve_copilot_created_by_stamp
from skyvern.forge.sdk.copilot.block_goal_wrapping import wrap_workflow_block_goals
from skyvern.forge.sdk.copilot.block_type_aliases import normalize_copilot_block_type_alias
from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
    RecoveryHint,
    build_loop_blocker_signal,
    clear_blocker_signal_for_reason_codes,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.build_phase import (
    BuildPhase,
    _phase_blocker_signal,
    advance_to_composing,
    advance_to_discovering,
    advance_to_testing,
)
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    CriterionVerdict,
    RunEvidenceSnapshot,
    evaluate_completion_criteria,
    summarize_unsatisfied_outcomes,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    COMPOSITION_STRIPPED_HTML_EXPRESSION as _COMPOSITION_STRIPPED_HTML_EXPRESSION,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    COMPOSITION_STRIPPED_HTML_MAX_CHARS as _COMPOSITION_STRIPPED_HTML_MAX_CHARS,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION as _COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    COMPOSITION_STRUCTURED_EVIDENCE_MAX_CHARS as _COMPOSITION_STRUCTURED_EVIDENCE_MAX_CHARS,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    COMPOSITION_VISUAL_OBSTRUCTION_CANDIDATES_EXPRESSION as _COMPOSITION_VISUAL_OBSTRUCTION_CANDIDATES_EXPRESSION,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    SCOUT_INTERACTION_EVIDENCE_TOOL,
    composition_page_evidence_error,
    has_bounded_page_schema,
    merge_visual_composition_evidence,
    normalize_block_observation_refs,
    page_evidence_needs_visual_fallback,
    parse_composition_html,
    parse_composition_structured,
    workflow_target_url,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, normalize_block_authoring_policy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisRepairContract,
    build_diagnosis_repair_contract,
)
from skyvern.forge.sdk.copilot.enforcement import (
    POST_INTERMEDIATE_SUCCESS_NUDGE,
    PROBABLE_SITE_BLOCK_STREAK_STOP_AT,
    TOTAL_TIMEOUT_SECONDS,
    _goal_likely_needs_more_blocks,
)
from skyvern.forge.sdk.copilot.failure_tracking import (
    ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY,
    ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE,
    PER_TOOL_BUDGET_FAILURE_CATEGORY,
    _canonical_block_config,
    compute_action_sequence_fingerprint,
    update_repeated_failure_state,
)
from skyvern.forge.sdk.copilot.llm_config import resolve_fast_copilot_handler, resolve_main_copilot_handler
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
from skyvern.forge.sdk.copilot.outcome_verification_trace import record_completion_verification
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
from skyvern.forge.sdk.copilot.request_policy import CREDENTIAL_DEFERRED_DRAFT_REASONS, RequestPolicy
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    PendingBrowserInteractionObservation,
    ScoutedInteraction,
    ensure_browser_session,
)
from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result
from skyvern.forge.sdk.copilot.streaming_adapter import emit_workflow_draft, maybe_emit_design_end
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.turn_intent import (
    NO_MUTATION_TURN_INTENT_MODES,
    READ_CONTEXT_DENIED_MODES,
    UNRESOLVED_BLOCK_REF_TARGET_ENTITY,
    TurnIntent,
    TurnIntentMode,
)
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from skyvern.forge.sdk.routes.workflow_copilot import _process_workflow_yaml
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException
from skyvern.forge.sdk.workflow.models.block import BlockTypeVar, get_all_blocks
from skyvern.forge.sdk.workflow.models.parameter import (
    RESERVED_PARAMETER_KEYS,
    OutputParameter,
    Parameter,
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
# Block types whose output can demonstrate an end-state outcome. Until a workflow
# contains one of these, an unmet outcome criterion means the build is still
# incomplete (no confirmation step yet), not a completed run that failed the goal.
_OUTCOME_EVIDENCE_BLOCK_TYPES = frozenset({BlockType.EXTRACTION.value, BlockType.VALIDATION.value})

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
# 900 s) so a long chain cannot consume the whole budget without giving the
# copilot a chance to issue a smaller chain. Quiet-block runs keep the longer
# ``RUN_BLOCKS_SAFETY_CEILING_SECONDS`` above.
PER_TOOL_CALL_BUDGET_SECONDS = 240
_ACTIVE_RUN_TERMINAL_MONITOR_INITIAL_DELAY_SECONDS = 30.0
_ACTIVE_RUN_TERMINAL_MONITOR_INTERVAL_SECONDS = 30.0
_ACTIVE_RUN_TERMINAL_MONITOR_MAX_SAMPLES = 8

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
_EVALUATE_EVIDENCE_CONFIDENCE_WITH_SCHEMA = 0.75
_EVALUATE_EVIDENCE_CONFIDENCE_ANTIBOT_ONLY = 0.4


@dataclass(frozen=True)
class ActiveRunTerminalEvidenceSample:
    current_url: str | None
    page_title: str | None
    page_evidence: dict[str, Any]
    completion_verification: CompletionVerificationResult
    sample_index: int


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


def _credential_parameter_slot_field(parameter: Any) -> str | None:
    """Return the field name that legitimately carries a `cred_xxx` value for
    this parameter dict, or None if the parameter is not a credential-binding
    slot. Two shapes resolve a credential at runtime: a top-level or block-level
    `parameter_type: credential` with the ID in `credential_id`, and a
    `parameter_type: workflow` + `workflow_parameter_type: credential_id` with
    the ID in `default_value`.
    """
    if not isinstance(parameter, dict):
        return None
    parameter_type = str(parameter.get("parameter_type") or "").lower()
    if parameter_type == "credential":
        return "credential_id"
    workflow_parameter_type = str(parameter.get("workflow_parameter_type") or "").lower()
    if parameter_type == "workflow" and workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID.value:
        return "default_value"
    return None


def _extract_credential_ids_from_workflow_parameters(parameters: Any) -> list[str]:
    if not isinstance(parameters, list):
        return []

    found: list[str] = []
    for parameter in parameters:
        slot_field = _credential_parameter_slot_field(parameter)
        if slot_field is None:
            continue
        found.extend(_extract_credential_ids_from_tool_value(parameter.get(slot_field)))

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


def _parsed_workflow_definition(workflow_yaml: str | None) -> dict[str, Any] | None:
    if not workflow_yaml:
        return None
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return None
    return workflow_definition


def _extract_credential_ids_from_workflow_yaml(workflow_yaml: str | None) -> list[str]:
    workflow_definition = _parsed_workflow_definition(workflow_yaml)
    if workflow_definition is None:
        return []
    return _extract_credential_ids_from_workflow_parameters(workflow_definition.get("parameters"))


_MISBINDING_WORKFLOW_LOCATION = "workflow"


def _credential_id_misbinding_findings(workflow_yaml: str | None) -> list[dict[str, str]]:
    workflow_definition = _parsed_workflow_definition(workflow_yaml)
    if workflow_definition is None:
        return []

    findings: list[dict[str, str]] = []

    def _scan_value(value: Any, location: str, field: str) -> None:
        if isinstance(value, str):
            for credential_id in _CREDENTIAL_ID_RE.findall(value):
                findings.append({"location": location, "field": field, "credential_id": credential_id})
        elif isinstance(value, list):
            for item in value:
                _scan_value(item, location, field)
        elif isinstance(value, dict):
            for nested_field, nested_value in value.items():
                _scan_value(nested_value, location, str(nested_field))

    def _scan_parameter(parameter: Any, location: str) -> None:
        if not isinstance(parameter, dict):
            return
        legal_slot_field = _credential_parameter_slot_field(parameter)
        for field_name, field_value in parameter.items():
            if field_name == legal_slot_field:
                continue
            _scan_value(field_value, location, str(field_name))

    for parameter in workflow_definition.get("parameters") or []:
        _scan_parameter(parameter, _MISBINDING_WORKFLOW_LOCATION)

    for block in _iter_yaml_blocks(workflow_definition.get("blocks")):
        label = str(block.get("label") or "<unlabeled>")
        for field_name, field_value in block.items():
            if field_name == "parameters":
                if isinstance(field_value, list):
                    for parameter in field_value:
                        _scan_parameter(parameter, label)
                continue
            if field_name == "loop_blocks":
                continue
            _scan_value(field_value, label, str(field_name))

    return findings


def _credential_id_misbinding_error_message(findings: list[dict[str, str]]) -> str:
    grouped: dict[tuple[str, str], list[str]] = {}
    for finding in findings:
        key = (finding["location"], finding["credential_id"])
        grouped.setdefault(key, []).append(finding["field"])

    location_lines: list[str] = []
    for (location, credential_id), fields in grouped.items():
        unique_fields = list(dict.fromkeys(fields))
        joined = ", ".join(f"`{field}`" for field in unique_fields)
        scope = "workflow parameter" if location == _MISBINDING_WORKFLOW_LOCATION else f"block `{location}`"
        location_lines.append(f"- `{credential_id}` in {scope} field(s): {joined}")
    body = "\n".join(location_lines)

    return (
        "A credential ID is sitting in workflow fields that do not resolve it, so at runtime the agent types "
        "the literal ID into the page instead of the stored username/password:\n"
        f"{body}\n"
        "Fix BOTH halves before retrying:\n"
        "1. Bind the credential once: add a `credential` parameter (or a `workflow` parameter with "
        "`workflow_parameter_type: credential_id` and the ID in `default_value`) and reference its key from the "
        "login block's `parameter_keys`.\n"
        "2. Delete the credential ID string from every field listed above. `navigation_goal`, "
        "`complete_criterion`, `terminate_criterion` and similar fields are plain-language instructions — they "
        "must describe the outcome without naming the credential ID. Do NOT relocate the literal ID into another "
        "prose or list field; only the credential parameter slot may hold it."
    )


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


def _guardrail_tool_arguments(tool_context: Any) -> tuple[dict[str, Any], Any]:
    raw_arguments = getattr(tool_context, "tool_arguments", "")
    try:
        # Agents SDK guardrails may hand us either raw JSON or an already parsed mapping.
        parsed_arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
    except json.JSONDecodeError:
        parsed_arguments = {}
    return parsed_arguments if isinstance(parsed_arguments, dict) else {}, raw_arguments


def _workflow_yaml_output_policy_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    tool_context = data.context
    tool_arguments, raw_arguments = _guardrail_tool_arguments(tool_context)
    if not raw_arguments:
        LOG.warning(
            "workflow YAML output policy guardrail received no tool arguments",
            tool_name=getattr(tool_context, "tool_name", None),
            tool_call_id=getattr(tool_context, "tool_call_id", None),
        )
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

_COMPOSITION_EVIDENCE_PRECHECK_TRACE_DATA = {
    "surface": "tool_pre_side_effect",
    "tool_name": "update_and_run_blocks",
    "reason": "composition_page_evidence",
}


def _update_and_run_blocks_composition_evidence_precheck(
    copilot_ctx: Any,
    workflow_yaml: str | None,
    normalized_block_observation_refs: dict[str, int],
    raw_block_observation_refs: Any,
) -> str | None:
    if copilot_ctx is None or workflow_yaml is None:
        LOG.warning(
            "update_and_run_blocks composition evidence precheck missing context or workflow yaml",
            has_context=copilot_ctx is not None,
            has_workflow_yaml=workflow_yaml is not None,
        )
        return None

    evidence_error = composition_page_evidence_error(
        copilot_ctx,
        workflow_yaml,
        block_observation_refs=normalized_block_observation_refs,
        raw_block_observation_refs=raw_block_observation_refs,
    )

    if evidence_error:
        LOG.info(
            "copilot composition page evidence pre-side-effect rejected workflow",
            workflow_permanent_id=getattr(copilot_ctx, "workflow_permanent_id", None),
            target_url=workflow_target_url(workflow_yaml),
            surface="tool_pre_side_effect",
        )
        return evidence_error

    return None


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
    internal_watchdog_cancel_category = getattr(copilot_ctx, "last_failure_category_top", None) in {
        PER_TOOL_BUDGET_FAILURE_CATEGORY,
        ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY,
    }
    if is_trusted_final or internal_watchdog_cancel_category:
        copilot_ctx.pending_reconciliation_run_id = None
        copilot_ctx.pending_reconciliation_requires_user_input = False
        clear_blocker_signal_for_reason_codes(
            copilot_ctx,
            frozenset(
                {
                    "tool_error_pending_reconciliation_no_input",
                    "tool_error_pending_reconciliation_requires_input",
                }
            ),
        )
        return
    if resolved_status == WorkflowRunStatus.canceled.value:
        copilot_ctx.pending_reconciliation_requires_user_input = True
        # Replace the no_input blocker with the requires-input one; unrelated
        # blockers (e.g. `loop_detected`) survive the targeted clear.
        existing_blocker = getattr(copilot_ctx, "blocker_signal", None)
        # Preserve the original blocked_tool so trace queries filtering on it correlate the no_input → requires_input transition.
        original_blocked_tool = (
            existing_blocker.blocked_tool
            if (
                isinstance(existing_blocker, CopilotToolBlockerSignal)
                and existing_blocker.internal_reason_code == "tool_error_pending_reconciliation_no_input"
                and existing_blocker.blocked_tool
            )
            else "get_run_results"
        )
        clear_blocker_signal_for_reason_codes(
            copilot_ctx,
            frozenset({"tool_error_pending_reconciliation_no_input"}),
        )
        stash_blocker_signal(
            copilot_ctx,
            _pending_reconciliation_requires_input_signal(
                pending_run_id=pending_run_id,
                blocked_tool=original_blocked_tool,
            ),
        )


def _mark_pending_reconciliation_run(copilot_ctx: Any, workflow_run_id: str) -> None:
    copilot_ctx.pending_reconciliation_run_id = workflow_run_id
    copilot_ctx.pending_reconciliation_requires_user_input = False


def _workflow_verification_evidence(ctx: AgentContext) -> WorkflowVerificationEvidence:
    return ctx.workflow_verification_evidence


def _run_result_label_list(data: Mapping[str, object], key: str) -> list[str]:
    values = data.get(key)
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _run_result_blocks(data: Mapping[str, object]) -> list[Mapping[str, object]]:
    blocks = data.get("blocks")
    return [block for block in blocks if isinstance(block, dict)] if isinstance(blocks, list) else []


def _completed_run_block_labels(data: Mapping[str, object]) -> list[str]:
    labels = [
        str(block.get("label") or "").strip()
        for block in _run_result_blocks(data)
        if _enum_or_string_name(block.get("status")) == WorkflowRunStatus.completed.value
    ]
    labels = list(dict.fromkeys(label for label in labels if label))
    return labels or _run_result_label_list(data, "executed_block_labels")


def _failed_run_block_labels(data: Mapping[str, object]) -> list[str]:
    labels = [
        str(block.get("label") or "").strip()
        for block in _run_result_blocks(data)
        if _enum_or_string_name(block.get("status")) in _FAILED_BLOCK_STATUSES
    ]
    labels = list(dict.fromkeys(label for label in labels if label))
    if labels:
        return labels
    frontier = data.get("frontier_start_label")
    return [frontier] if isinstance(frontier, str) and frontier.strip() else []


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
    if has_indicators:
        disabled_controls = _disabled_submit_controls_from_evidence(evidence)
        if disabled_controls:
            labels = ", ".join(_composition_control_label(control) for control in disabled_controls[:3])
            return f"anti-bot evidence remains while submit/search control(s) are disabled ({labels})"
    return None


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

    requested_labels = _requested_block_label_set(arguments)
    labels_text = f" Requested labels: {', '.join(sorted(requested_labels))}." if requested_labels else ""
    agent_steering = (
        "The prior block-running tool hit a failed/budgeted frontier, and bounded current-page inspection "
        f"now shows: {reason}.{labels_text} Do NOT call "
        f"{tool_name} again in this turn, do NOT try another proxy/location from this evidence state, and "
        "do NOT claim registry results or no-results were verified. REPLY now with a blocker explanation "
        "that names the observed challenge/disabled control and summarizes the tested workflow state."
    )
    user_facing = (
        "The site's verification challenge is still blocking the submit/search control after live-page inspection, "
        "so I stopped without claiming results."
    )
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=agent_steering,
        user_facing_reason=user_facing,
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code="tool_error_post_budget_challenge_blocker",
        blocked_tool=tool_name,
    )


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
    "captcha",
    "challenge",
    "human verification",
    "verify you are human",
)


def _normalize_structured_key(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _looks_like_anti_bot_blocker(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in _ANTI_BOT_BLOCKER_TERMS)


def _structured_blocker_message(value: object, *, depth: int = 0) -> str | None:
    if depth > 5:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = _normalize_structured_key(key)
            if not isinstance(item, str) or not item.strip():
                continue
            has_blocker_key = normalized_key in _STRUCTURED_BLOCKER_MESSAGE_KEYS or any(
                term in normalized_key for term in _STRUCTURED_BLOCKER_KEY_TERMS
            )
            if has_blocker_key or (
                normalized_key in {"message", "error", "failure_reason", "reason"}
                and _looks_like_anti_bot_blocker(item)
            ):
                return item.strip()[:240]
        for item in value.values():
            nested = _structured_blocker_message(item, depth=depth + 1)
            if nested:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _structured_blocker_message(item, depth=depth + 1)
            if nested:
                return nested
    return None


def _run_blocks_structured_blocker_message(result: dict[str, Any]) -> str | None:
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
        if block_type not in _DATA_PRODUCING_BLOCK_TYPES or block.get("status") != "completed":
            continue
        payload = _block_data_payload(block.get("extracted_data"), block_type)
        blocker = _structured_blocker_message(payload)
        if blocker:
            return blocker
    return None


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
WORKFLOW_MUTATION_TOOLS = frozenset({"update_workflow", "update_and_run_blocks"})
ANSWER_ONLY_CONTEXT_TOOLS = frozenset({"get_run_results"})
CREDENTIAL_METADATA_TOOLS = frozenset({"list_credentials"})
PAGE_INSPECTION_TOOLS = frozenset({"inspect_page_for_composition", "evaluate", "get_browser_screenshot"})
PAGE_SCHEMA_CONTEXT_TOOLS = frozenset({"inspect_page_for_composition"})
_CURRENT_PAGE_INSPECTION_TARGETS = frozenset({"", "current", "current_page", "__current_page__"})


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


def _mark_page_inspected(ctx: AgentContext) -> None:
    ctx.post_budget_page_inspection_required = False
    ctx.post_budget_page_inspection_url = None
    ctx.post_budget_page_inspection_run_id = None


def _same_page_ignoring_fragment(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        left_parsed = urlparse(left)
        right_parsed = urlparse(right)
    except Exception as exc:
        LOG.debug("copilot_same_page_url_parse_failed", left=left, right=right, error=str(exc))
        return False
    left_url = left_parsed._replace(fragment="").geturl().rstrip("/")
    right_url = right_parsed._replace(fragment="").geturl().rstrip("/")
    return left_url == right_url


def _clear_pending_browser_interaction_observation(ctx: AgentContext) -> None:
    ctx.pending_browser_interaction_observation = None


def _mark_pending_browser_interaction_observation(ctx: AgentContext, *, tool_name: str, url: str) -> None:
    if not url.strip():
        _clear_pending_browser_interaction_observation(ctx)
        return
    ctx.pending_browser_interaction_observation = PendingBrowserInteractionObservation(
        tool_name=tool_name,
        url=url.strip(),
    )


def _consume_pending_browser_interaction_observation(
    ctx: AgentContext,
    *,
    current_url: str,
    evidence: dict[str, Any],
) -> bool:
    pending = ctx.pending_browser_interaction_observation
    if pending is None:
        return False
    _clear_pending_browser_interaction_observation(ctx)
    if not has_bounded_page_schema(evidence):
        return False
    if not _same_page_ignoring_fragment(pending.url, current_url):
        LOG.warning(
            "copilot_pending_browser_interaction_observation_page_mismatch",
            tool_name=pending.tool_name,
            pending_url=pending.url,
            current_url=current_url,
        )
        return False
    return True


_MAX_SCOUTED_INTERACTIONS = 20


async def _live_working_page_url(ctx: AgentContext) -> str | None:
    if not ctx.browser_session_id:
        return None
    try:
        browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
            session_id=ctx.browser_session_id,
            organization_id=ctx.organization_id,
        )
        if not browser_state:
            return None
        page = await browser_state.get_or_create_page()
        return page.url if page else None
    except Exception:
        return None


async def _capture_scout_source_url(ctx: AgentContext) -> None:
    # Pre-action: a navigating click/Enter would leave only the destination URL, not the page the selector acted on.
    ctx.pending_scout_source_url = await _live_working_page_url(ctx)


def _consume_scout_source_url(ctx: AgentContext) -> str | None:
    source_url = ctx.pending_scout_source_url
    # Cleared unconditionally so a non-recording action can't bleed its source page into a later interaction.
    ctx.pending_scout_source_url = None
    return source_url


def _record_scouted_interaction(
    ctx: AgentContext,
    *,
    tool_name: str,
    selector: str = "",
    source_url: str | None = None,
    value: str = "",
    key: str = "",
    typed_length: int = 0,
) -> None:
    selector = selector.strip()
    # press_key may be page-level, so it is recorded by key even with no selector; other tools require one.
    if tool_name != "press_key" and not selector:
        return
    artifact: ScoutedInteraction = {"tool_name": tool_name}
    if selector:
        artifact["selector"] = selector
    if source_url and source_url.strip():
        artifact["source_url"] = source_url.strip()
    if value:
        artifact["value"] = value
    if key:
        artifact["key"] = key
    if typed_length:
        artifact["typed_length"] = typed_length
    interactions = [
        item
        for item in ctx.scouted_interactions
        if not (
            item.get("tool_name") == artifact["tool_name"]
            and item.get("selector") == artifact.get("selector")
            and item.get("source_url") == artifact.get("source_url")
        )
    ]
    interactions.append(artifact)
    ctx.scouted_interactions = interactions[-_MAX_SCOUTED_INTERACTIONS:]
    LOG.info(
        "copilot_scout_interaction_captured",
        tool_name=tool_name,
        selector=selector or None,
        source_url=artifact.get("source_url"),
        total_scouted_interactions=len(ctx.scouted_interactions),
    )


def _register_scout_interaction_observation(
    ctx: AgentContext, *, tool_name: str, selector: str, source_url: str | None, url: str
) -> int | None:
    # A successful scout interaction reaches the post-action page; record it as an
    # interaction-reached observation so a click-reached block can be authored
    # against it without a separate inspect_page_for_composition.
    selector = selector.strip()
    if not selector or not url:
        return None
    evidence: dict[str, Any] = {
        "inspected_url": url,
        "current_url": url,
        "source_tool": SCOUT_INTERACTION_EVIDENCE_TOOL,
        "interaction_tool": tool_name,
        "interaction_selector": selector,
    }
    if source_url and source_url.strip():
        evidence["interaction_source_url"] = source_url.strip()
    return _append_flow_evidence(ctx, evidence, reached_via="interaction")


def _mark_post_run_page_observed(ctx: AgentContext, *, source_tool: str, url: str) -> None:
    run_id = getattr(ctx, "last_run_blocks_workflow_run_id", None)
    if not isinstance(run_id, str) or not run_id:
        return
    ctx.post_run_page_observation_tool = source_tool
    ctx.post_run_page_observation_url = url
    ctx.post_run_page_observation_workflow_run_id = run_id
    ctx.post_run_page_observation_after_failed_test = getattr(ctx, "last_test_ok", None) is False
    evidence = _workflow_verification_evidence(ctx)
    evidence.live_page_state_verified = True
    evidence.verified_from_current_browser_state = True
    evidence.workflow_run_id = run_id
    if url:
        evidence.current_url = url
        evidence.current_url_observed_after_workflow_run = True
        evidence.current_url_may_encode_runtime_state = bool(urlparse(url).query)


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
    detected = detect_failed_tool_step_loop_for_ctx(ctx, tool_name, arguments or {})
    if detected is not None:
        return _emit_tool_blocker_signal(ctx, _build_loop_blocker_signal(detected, tool_name=tool_name))

    # Consecutive same-name guard: false-positives on the intended iterative
    # build (one new block per update_and_run_blocks). Block-running tools
    # rely on the progress-aware checks below instead.
    tracker = getattr(ctx, "consecutive_tool_tracker", None)
    if isinstance(tracker, list) and tool_name not in BLOCK_RUNNING_TOOLS:
        detected = detect_tool_loop(tracker, tool_name)
        if detected is not None:
            return _emit_tool_blocker_signal(ctx, _build_loop_blocker_signal(detected, tool_name=tool_name))

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
    return None


_build_loop_blocker_signal = build_loop_blocker_signal


def _request_policy_tool_error(ctx: AgentContext, tool_name: str) -> CopilotToolBlockerSignal | None:
    policy = getattr(ctx, "request_policy", None)
    if not isinstance(policy, RequestPolicy):
        return None

    agent_steering: str | None = None
    user_facing: str | None = None
    reason_code: str | None = None
    recovery_hint: RecoveryHint = "report_blocker_to_user"
    cleared_by: frozenset[str] = frozenset()

    if tool_name == "update_workflow" and not policy.allow_update_workflow:
        reason_code = "request_policy_blocks_update_workflow"
        agent_steering = (
            "Request policy blocks workflow updates for the latest user message. "
            "Ask the user for safe stored credential metadata instead."
        )
        user_facing = "I need stored credential metadata before I can update this workflow."
        recovery_hint = "ask_user_clarifying"
    elif tool_name in BLOCK_RUNNING_TOOLS and not policy.allow_run_blocks:
        if policy.testing_intent == "skip_test":
            reason_code = "request_policy_blocks_run_blocks_skip_test"
            agent_steering = (
                "Request policy says the latest user message asked for an untested draft. Use update_workflow only."
            )
            user_facing = "I'll save this as an untested draft because the request asked me not to run it."
            recovery_hint = "retry_with_different_tool"
            cleared_by = frozenset({"update_workflow"})
        elif policy.clarification_reason == "workflow_credential_inputs_unbound":
            reason_code = "request_policy_blocks_run_blocks_credential_unbound"
            agent_steering = (
                "Skipped test run: the existing workflow references credential parameters "
                "whose keys point to workflow inputs that are not configured. REPLY to the user "
                "with: 'I applied your requested change. I couldn't test the modified workflow "
                "because I couldn't find the required credentials — please add them via the "
                "Credentials UI, then I can try again.' Keep the unvalidated draft surfaced."
            )
            user_facing = (
                "I applied your requested change. I couldn't test the modified workflow because "
                "the required credentials aren't set up. Add them in the Credentials UI and ask "
                "me to test it."
            )
            recovery_hint = "report_blocker_to_user"
        else:
            reason_code = "request_policy_blocks_run_blocks_generic"
            agent_steering = (
                "Request policy blocks block-running tools for the latest user message. "
                "Ask the user for the required safe credential or clarification before testing."
            )
            user_facing = "I need a credential or clarification from you before I can test this workflow."
            recovery_hint = "ask_user_clarifying"

    if reason_code is None or agent_steering is None or user_facing is None:
        return None

    LOG.info(
        "copilot authority gate evaluated tool",
        authority_gate_layer="request_policy",
        blocked_tool=tool_name,
        request_policy_allow_update_workflow=policy.allow_update_workflow,
        request_policy_allow_run_blocks=policy.allow_run_blocks,
        request_policy_testing_intent=policy.testing_intent,
        request_policy_clarification_reason=policy.clarification_reason,
        safe_reason_code=reason_code,
    )
    return CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text=agent_steering,
        user_facing_reason=user_facing,
        recovery_hint=recovery_hint,
        cleared_by_tools=cleared_by,
        internal_reason_code=reason_code,
        blocked_tool=tool_name,
    )


def _request_policy_allows_credential_deferred_draft(ctx: AgentContext) -> bool:
    policy = getattr(ctx, "request_policy", None)
    return (
        isinstance(policy, RequestPolicy)
        and policy.allow_update_workflow
        and not policy.allow_run_blocks
        and policy.allow_missing_credentials_in_draft
        and policy.clarification_reason in CREDENTIAL_DEFERRED_DRAFT_REASONS
    )


def _request_policy_allows_update_and_skip_run(ctx: AgentContext, tool_name: str) -> bool:
    return tool_name == "update_and_run_blocks" and _request_policy_allows_credential_deferred_draft(ctx)


def _turn_intent_has_edit_target(intent: TurnIntent) -> bool:
    # Keep this aligned with TurnIntent target kinds that make an edit specific enough to mutate safely.
    if any(
        intent.target_entities.get(entity_type)
        for entity_type in (
            "block",
            "run",
            "proposed_workflow",
            "latest_assistant_proposal",
            "proposal",
            "workflow_change",
        )
    ):
        return True
    return any(target != "current_workflow" for target in intent.target_entities.get("workflow", []))


def _turn_intent_tool_error(ctx: AgentContext, tool_name: str) -> CopilotToolBlockerSignal | None:
    intent = getattr(ctx, "turn_intent", None)
    if not isinstance(intent, TurnIntent):
        return None

    authority = intent.authority
    unresolved_refs = intent.target_entities.get(UNRESOLVED_BLOCK_REF_TARGET_ENTITY, [])
    if intent.mode == TurnIntentMode.EDIT and tool_name in WORKFLOW_MUTATION_TOOLS and unresolved_refs:
        reason_code = "turn_intent_unresolved_edit_target"
        labels = sorted(_workflow_yaml_blocks_by_label(getattr(ctx, "workflow_yaml", None)))
        label_hint = ", ".join(labels[:8]) if labels else "no labeled blocks"
        LOG.info(
            "copilot authority gate evaluated tool",
            authority_gate_layer="turn_intent",
            turn_intent_mode=intent.mode.value,
            turn_intent_target_entity_types=sorted(intent.target_entities),
            turn_intent_unresolved_refs=unresolved_refs,
            blocked_tool=tool_name,
            safe_reason_code=reason_code,
        )
        unresolved_str = ", ".join(unresolved_refs)
        return _build_turn_intent_signal(
            tool_name=tool_name,
            classifier_mode=intent.mode.value,
            reason_code=reason_code,
            agent_steering_text=(
                f"The latest user message references workflow/block identifier(s) that are not present in the "
                f"current workflow: {unresolved_str}. Current workflow labels include: {label_hint}. Ask the user "
                f"which current block should change before mutating or running blocks."
            ),
            user_facing_reason=(
                f"I couldn't find the block(s) you mentioned ({unresolved_str}). "
                f"Tell me which existing block to change."
            ),
            recovery_hint="ask_user_clarifying",
        )

    if (
        intent.mode == TurnIntentMode.EDIT
        and tool_name in WORKFLOW_MUTATION_TOOLS
        and not _turn_intent_has_edit_target(intent)
    ):
        reason_code = "turn_intent_missing_edit_target"
        LOG.info(
            "copilot authority gate evaluated tool",
            authority_gate_layer="turn_intent",
            turn_intent_mode=intent.mode.value,
            turn_intent_target_entity_types=sorted(intent.target_entities),
            blocked_tool=tool_name,
            safe_reason_code=reason_code,
        )
        return _build_turn_intent_signal(
            tool_name=tool_name,
            classifier_mode=intent.mode.value,
            reason_code=reason_code,
            agent_steering_text=(
                "Could not identify a specific workflow edit target. Ask the user which workflow/block should "
                "change before mutating."
            ),
            user_facing_reason="Tell me which block or workflow you'd like me to change.",
            recovery_hint="ask_user_clarifying",
        )

    blocks_update = tool_name in WORKFLOW_MUTATION_TOOLS and not authority.may_update_workflow
    blocks_run = tool_name in BLOCK_RUNNING_TOOLS and not authority.may_run_blocks
    blocks_page_inspection = tool_name in PAGE_SCHEMA_CONTEXT_TOOLS and (
        intent.mode in NO_MUTATION_TURN_INTENT_MODES or not authority.may_update_workflow
    )
    blocks_credential_metadata = tool_name in CREDENTIAL_METADATA_TOOLS and not (
        authority.may_update_workflow or authority.may_run_blocks
    )
    # Two paths grant read access to ANSWER_ONLY_CONTEXT_TOOLS, both excluded for DOCS_ANSWER/REFUSE/CLARIFY:
    #  (1) authority.may_read_run_context — classifier-derived (DIAGNOSE turns)
    #  (2) pending_reconciliation_run_id — within-turn override anchored to the run-blocks watchdog
    may_read_run_context = authority.may_read_run_context and intent.mode not in READ_CONTEXT_DENIED_MODES
    blocks_context_read = tool_name in ANSWER_ONLY_CONTEXT_TOOLS and not may_read_run_context

    within_turn_read_override = False
    if blocks_context_read and intent.mode not in READ_CONTEXT_DENIED_MODES:
        pending_run_id = getattr(ctx, "pending_reconciliation_run_id", None)
        if isinstance(pending_run_id, str) and pending_run_id:
            blocks_context_read = False
            within_turn_read_override = True
        else:
            same_turn_run_id = getattr(ctx, "last_successful_run_blocks_workflow_run_id", None) or getattr(
                ctx,
                "last_run_blocks_workflow_run_id",
                None,
            )
            if isinstance(same_turn_run_id, str) and same_turn_run_id:
                blocks_context_read = False
                within_turn_read_override = True

    if blocks_run and not blocks_update and _request_policy_allows_update_and_skip_run(ctx, tool_name):
        return None
    if (
        not blocks_update
        and not blocks_run
        and not blocks_context_read
        and not blocks_page_inspection
        and not blocks_credential_metadata
    ):
        if within_turn_read_override:
            LOG.info(
                "copilot authority gate allowed tool via within-turn read override",
                authority_gate_layer="turn_intent",
                turn_intent_mode=intent.mode.value,
                tool_name=tool_name,
                turn_intent_within_turn_read_override=True,
            )
        return None

    if intent.mode in NO_MUTATION_TURN_INTENT_MODES and blocks_run:
        reason_code = "turn_intent_no_mutation_run_blocked"
    elif intent.mode in NO_MUTATION_TURN_INTENT_MODES and blocks_update:
        reason_code = "turn_intent_no_mutation_update_blocked"
    elif blocks_update and blocks_run:
        reason_code = "turn_intent_no_mutation_run_blocked"
    elif blocks_update:
        reason_code = "turn_intent_update_blocked"
    elif blocks_page_inspection:
        reason_code = "turn_intent_page_inspection_blocked"
    elif blocks_context_read:
        reason_code = "turn_intent_context_read_blocked"
    elif blocks_credential_metadata:
        reason_code = "turn_intent_credential_metadata_blocked"
    else:
        reason_code = "turn_intent_run_blocked"
    LOG.info(
        "copilot authority gate evaluated tool",
        authority_gate_layer="turn_intent",
        turn_intent_mode=intent.mode.value,
        turn_intent_target_entity_types=sorted(intent.target_entities),
        turn_intent_may_update_workflow=authority.may_update_workflow,
        turn_intent_may_run_blocks=authority.may_run_blocks,
        turn_intent_may_read_run_context=authority.may_read_run_context,
        blocked_tool=tool_name,
        safe_reason_code=reason_code,
    )
    action = "ask the user" if authority.requires_user_input else "answer the user"
    detail = f" Ask: {intent.missing_context_question}" if intent.missing_context_question else ""

    if blocks_run and not blocks_update and authority.may_update_workflow:
        return _build_turn_intent_signal(
            tool_name=tool_name,
            classifier_mode=intent.mode.value,
            reason_code=reason_code,
            agent_steering_text=(
                "Browser blocks may not run for the latest user message. Use update_workflow only and keep the "
                f"draft unvalidated.{detail}"
            ),
            user_facing_reason="I'll save the change as a draft without running it.",
            recovery_hint="retry_with_different_tool",
            cleared_by_tools=frozenset({"update_workflow"}),
        )
    if blocks_context_read and not blocks_update and not blocks_run:
        return _build_turn_intent_signal(
            tool_name=tool_name,
            classifier_mode=intent.mode.value,
            reason_code=reason_code,
            agent_steering_text=(
                "Run context may not be read for the latest user message. Answer using the context already "
                f"provided.{detail}"
            ),
            user_facing_reason="I'll answer with the information I already have.",
            recovery_hint="report_blocker_to_user",
        )
    return _build_turn_intent_signal(
        tool_name=tool_name,
        classifier_mode=intent.mode.value,
        reason_code=reason_code,
        agent_steering_text=(
            "This tool is not allowed for the latest user message. Do not update workflow YAML or run browser "
            "blocks, and do not fetch additional run context with tools; "
            f"{action} using the available context instead.{detail}"
        ),
        user_facing_reason="I'll respond with the information I already have.",
        recovery_hint="ask_user_clarifying" if authority.requires_user_input else "report_blocker_to_user",
    )


def _build_turn_intent_signal(
    *,
    tool_name: str,
    classifier_mode: str,
    reason_code: str,
    agent_steering_text: str,
    user_facing_reason: str,
    recovery_hint: RecoveryHint,
    cleared_by_tools: frozenset[str] = frozenset(),
) -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text=agent_steering_text,
        user_facing_reason=user_facing_reason,
        recovery_hint=recovery_hint,
        cleared_by_tools=cleared_by_tools,
        internal_reason_code=reason_code,
        blocked_tool=tool_name,
        classifier_mode=classifier_mode,
    )


def _emit_tool_blocker_signal(ctx: AgentContext, signal: CopilotToolBlockerSignal) -> str:
    return stash_blocker_signal(ctx, signal)


def _authority_tool_error(
    ctx: AgentContext,
    tool_name: str,
    *,
    ignore_request_policy_error: bool = False,
) -> str | None:
    # Request-policy precedes turn-intent unless explicitly ignored.
    turn_intent_signal = _turn_intent_tool_error(ctx, tool_name)
    request_policy_signal = _request_policy_tool_error(ctx, tool_name)
    if turn_intent_signal is not None and request_policy_signal is not None:
        LOG.info(
            "copilot authority gate blocked tool",
            authority_gate_layer="both",
            blocked_tool=tool_name,
        )
    chosen = (
        request_policy_signal
        if (request_policy_signal is not None and not ignore_request_policy_error)
        else turn_intent_signal
    )
    if chosen is not None:
        return _emit_tool_blocker_signal(ctx, chosen)
    phase_signal = _phase_blocker_signal(ctx, tool_name)
    if phase_signal is not None:
        LOG.info(
            "copilot authority gate blocked tool",
            authority_gate_layer="build_phase",
            blocked_tool=tool_name,
            build_phase=getattr(getattr(ctx, "build_phase", None), "value", None),
        )
        return _emit_tool_blocker_signal(ctx, phase_signal)
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


class BlockObservationRef(BaseModel):
    label: str
    observation_step: Annotated[int, Field(ge=0)]


ArtifactEvidenceStatus = Literal["satisfied", "missing", "diagnostic_only", "observed_not_verified"]


class CodeArtifactClaimedOutcome(BaseModel):
    id: str
    scope: str
    text: str
    status: ArtifactEvidenceStatus
    depends_on: list[str] = Field(default_factory=list)
    covered_criteria: list[str] = Field(default_factory=list)
    criteria_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    observation_refs: list[str] = Field(default_factory=list)
    required_tokens: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)


class CodeArtifactPageDependency(BaseModel):
    id: str
    scope: str
    status: ArtifactEvidenceStatus
    url_hint: str | None = None
    page_state_hint: str | None = None
    required_affordances: list[str] = Field(default_factory=list)
    required_outcomes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list, description="Dependency-scoped evidence_ref ids.")
    observation_refs: list[str] = Field(default_factory=list, description="Dependency-scoped observation_ref ids.")


class CodeArtifactCompletionCriterion(BaseModel):
    id: str
    text: str
    level: Literal["terminal", "outcome", "prefix", "method"]
    outcome: str | None = None
    terminal: bool | None = None


class CodeArtifactScopedRef(BaseModel):
    claim_id: str | None = None
    dependency_id: str | None = None
    criterion_id: str | None = None
    evidence_ref: str | None = None
    observation_ref: str | None = None
    status: ArtifactEvidenceStatus = Field(validation_alias=AliasChoices("status", "evidence_status"))
    source_tool: str | None = None
    observation_step: Annotated[int, Field(ge=0)] | None = None
    run_sample_id: str | None = None
    current_url: str | None = None
    source_label: str | None = None
    checkpoint_next_mode: Literal["advance", "stop"] | None = None


class CodeArtifactTerminalVerifierExpectation(BaseModel):
    id: str
    text: str
    criteria_ids: list[str] = Field(default_factory=list)
    claimed_outcome_ids: list[str] = Field(default_factory=list)


class CodeArtifactExplorationObservation(BaseModel):
    id: str
    text: str
    status: Literal["observed_not_verified"] = Field(
        default="observed_not_verified",
        validation_alias=AliasChoices("status", "evidence_status"),
    )
    observation_ref: str | None = None
    source_tool: str | None = None
    observation_step: Annotated[int, Field(ge=0)] | None = None
    current_url: str | None = None
    source_label: str | None = None
    checkpoint_next_mode: Literal["stop"] | None = None


class CodeArtifactMetadata(BaseModel):
    artifact_id: str | None = None
    block_label: str | None = None
    block_id: str | None = None
    declared_goal: str
    claimed_outcomes: list[CodeArtifactClaimedOutcome] = Field(default_factory=list)
    page_dependencies: list[CodeArtifactPageDependency] = Field(default_factory=list)
    completion_criteria: list[CodeArtifactCompletionCriterion] = Field(default_factory=list)
    evidence_refs: list[CodeArtifactScopedRef] = Field(default_factory=list)
    observation_refs: list[CodeArtifactScopedRef] = Field(default_factory=list)
    terminal_verifier_expectations: list[CodeArtifactTerminalVerifierExpectation] = Field(default_factory=list)
    exploration_observations: list[CodeArtifactExplorationObservation] = Field(default_factory=list)


_CODE_ARTIFACT_REQUIRED_LIST_FIELDS = (
    "claimed_outcomes",
    "page_dependencies",
    "completion_criteria",
    "terminal_verifier_expectations",
)


def _code_artifact_metadata_as_tool_argument(
    metadata: list[CodeArtifactMetadata] | None,
) -> list[dict[str, Any]]:
    if not metadata:
        return []
    return [item.model_dump(mode="json", exclude_none=True) for item in metadata]


def _normalize_code_artifact_metadata(
    raw_metadata: Any,
    workflow_yaml: str,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    if raw_metadata in (None, [], {}):
        return {}, None
    items = _code_artifact_metadata_items(raw_metadata)
    code_blocks = _workflow_yaml_code_blocks_by_label(workflow_yaml)
    normalized: dict[str, dict[str, Any]] = {}
    for raw_item in items:
        try:
            metadata = (
                raw_item
                if isinstance(raw_item, CodeArtifactMetadata)
                else CodeArtifactMetadata.model_validate(raw_item)
            )
        except ValidationError as exc:
            return {}, f"Artifact metadata is malformed: {exc}"
        dumped = metadata.model_dump(mode="json", exclude_none=True)
        label = str(dumped.get("block_label") or "").strip()
        if not label:
            return {}, "Artifact metadata requires a non-empty `block_label` for each artifact."
        if label not in code_blocks:
            return {}, (
                f"Artifact metadata for `{label}` does not reference an existing code block label. "
                "Attach metadata only to authored code blocks."
            )
        if label in normalized:
            return {}, f"Artifact metadata contains duplicate entries for `{label}`."
        dumped["block_label"] = label
        artifact_id = str(dumped.get("artifact_id") or "").strip()
        if artifact_id and not artifact_id.startswith("code_artifact:"):
            return {}, f"Artifact metadata for `{label}` requires `artifact_id` to start with `code_artifact:`."
        dumped["artifact_id"] = artifact_id or _artifact_id_for_block_label(label)
        block_id = str(
            dumped.get("block_id") or code_blocks[label].get("block_id") or code_blocks[label].get("id") or ""
        ).strip()
        if block_id:
            dumped["block_id"] = block_id
        declared_goal = str(dumped.get("declared_goal") or "").strip()
        if not declared_goal:
            return {}, f"Artifact metadata for `{label}` requires a non-empty `declared_goal`."
        for field_name in _CODE_ARTIFACT_REQUIRED_LIST_FIELDS:
            value = dumped.get(field_name)
            if not isinstance(value, list) or not value:
                return {}, f"Artifact metadata for `{label}` requires non-empty `{field_name}`."
        if not dumped.get("evidence_refs") and not dumped.get("observation_refs"):
            return {}, f"Artifact metadata for `{label}` requires `evidence_refs` or `observation_refs`."
        validation_error = _code_artifact_metadata_shape_error(label, dumped)
        if validation_error is not None:
            return {}, validation_error
        normalized[label] = dumped
    return normalized, None


def _code_artifact_metadata_items(raw_metadata: Any) -> list[Any]:
    if isinstance(raw_metadata, Mapping):
        items: list[Any] = []
        for block_label, value in raw_metadata.items():
            if isinstance(value, Mapping) and "block_label" not in value:
                items.append({"block_label": block_label, **value})
            else:
                items.append(value)
        return items
    if isinstance(raw_metadata, list):
        return raw_metadata
    return [raw_metadata]


def _workflow_yaml_code_blocks_by_label(workflow_yaml: str | None) -> dict[str, Mapping[str, Any]]:
    blocks: dict[str, Mapping[str, Any]] = {}
    for label, block in _workflow_yaml_blocks_by_label(workflow_yaml).items():
        if _enum_or_string_name(block.get("block_type")) == BlockType.CODE.value:
            blocks[label] = block
    return blocks


def _artifact_id_for_block_label(label: str) -> str:
    fragment = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "artifact"
    return f"code_artifact:{fragment}"


def _code_artifact_metadata_shape_error(label: str, artifact: Mapping[str, Any]) -> str | None:
    for field_name, ref_key in (("evidence_refs", "evidence_ref"), ("observation_refs", "observation_ref")):
        for index, ref in enumerate(_artifact_rows(artifact.get(field_name))):
            if not str(ref.get(ref_key) or "").strip():
                return f"Artifact metadata for `{label}` `{field_name}` entry {index} requires `{ref_key}`."
            if not any(str(ref.get(key) or "").strip() for key in ("claim_id", "dependency_id", "criterion_id")):
                return f"Artifact metadata for `{label}` `{field_name}` entry {index} requires a scoped id."
            status = str(ref.get("status") or "").strip()
            if ref.get("checkpoint_next_mode") == "advance" and status != "diagnostic_only":
                return (
                    f"Artifact metadata for `{label}` `{field_name}` entry {index} has "
                    "`checkpoint_next_mode=advance`; it must stay `diagnostic_only`."
                )
            if ref.get("checkpoint_next_mode") == "stop" and status not in {"observed_not_verified", "diagnostic_only"}:
                return (
                    f"Artifact metadata for `{label}` `{field_name}` entry {index} has "
                    "`checkpoint_next_mode=stop`; it must remain `observed_not_verified` or `diagnostic_only`."
                )
            if status != "missing" and not str(ref.get("source_tool") or "").strip():
                return f"Artifact metadata for `{label}` `{field_name}` entry {index} requires `source_tool`."

    for index, claim in enumerate(_artifact_rows(artifact.get("claimed_outcomes"))):
        claim_id = str(claim.get("id") or "").strip()
        if not _artifact_string_list(claim.get("depends_on")):
            return f"Artifact metadata claim `{claim_id or index}` for `{label}` requires `depends_on`."
        claim_criteria = _artifact_string_list(claim.get("covered_criteria")) or _artifact_string_list(
            claim.get("criteria_ids")
        )
        if not claim_criteria:
            return f"Artifact metadata claim `{claim_id}` for `{label}` requires covered criterion ids."
        claim_evidence_refs = _artifact_string_list(claim.get("evidence_refs"))
        claim_observation_refs = _artifact_string_list(claim.get("observation_refs"))
        if claim.get("status") == "satisfied" and not claim_evidence_refs:
            return (
                f"Artifact metadata claim `{claim_id}` for `{label}` is `satisfied` but has no "
                "claim-scoped `evidence_refs`."
            )
        if claim.get("status") != "missing" and not claim_evidence_refs and not claim_observation_refs:
            return (
                f"Artifact metadata claim `{claim_id}` for `{label}` requires claim-scoped "
                "`evidence_refs` or `observation_refs` unless status is `missing`."
            )

    for dependency in _artifact_rows(artifact.get("page_dependencies")):
        dependency_id = str(dependency.get("id") or "").strip()
        dependency_evidence_refs = _artifact_string_list(dependency.get("evidence_refs"))
        dependency_observation_refs = _artifact_string_list(dependency.get("observation_refs"))
        if dependency.get("status") == "satisfied" and not dependency_evidence_refs:
            return (
                f"Artifact metadata dependency `{dependency_id}` for `{label}` is `satisfied` but has no "
                "dependency-scoped `evidence_refs`."
            )
        if dependency.get("status") != "missing" and not dependency_evidence_refs and not dependency_observation_refs:
            return (
                f"Artifact metadata dependency `{dependency_id}` for `{label}` requires scoped "
                "`evidence_refs` or `observation_refs` unless status is `missing`."
            )

    for index, expectation in enumerate(_artifact_rows(artifact.get("terminal_verifier_expectations"))):
        expectation_id = str(expectation.get("id") or "").strip()
        if not _artifact_string_list(expectation.get("criteria_ids")) and not _artifact_string_list(
            expectation.get("claimed_outcome_ids")
        ):
            return (
                f"Artifact metadata terminal verifier expectation `{expectation_id or index}` for `{label}` "
                "requires `criteria_ids` or `claimed_outcome_ids`."
            )

    for index, observation in enumerate(_artifact_rows(artifact.get("exploration_observations"))):
        if observation.get("status") != "observed_not_verified":
            return (
                f"Artifact metadata for `{label}` exploration observation {index} must be marked "
                "`observed_not_verified` until authored execution and terminal verification pass."
            )
        if observation.get("checkpoint_next_mode") == "advance":
            return (
                f"Artifact metadata for `{label}` exploration observation {index} cannot carry "
                "`checkpoint_next_mode=advance`; record that as `diagnostic_only` evidence instead."
            )
    return None


def _artifact_rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _artifact_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


async def _update_workflow(
    params: dict[str, Any],
    ctx: AgentContext,
    *,
    allow_missing_credentials: bool | None = None,
) -> dict[str, Any]:
    authority_error = _authority_tool_error(ctx, "update_workflow")
    if authority_error is not None:
        return {"ok": False, "error": authority_error}

    workflow_yaml = params["workflow_yaml"]
    # Tool wrappers run authority/loop guards before calling here. The composition
    # gate below consumes these refs, so they must be visible before validation.
    ctx.raw_block_observation_refs = params.get("raw_block_observation_refs", params.get("block_observation_refs"))
    ctx.block_observation_refs = normalize_block_observation_refs(params.get("block_observation_refs"))
    ctx.raw_code_artifact_metadata = params.get("raw_code_artifact_metadata", params.get("code_artifact_metadata"))
    code_artifact_metadata, code_artifact_metadata_error = _normalize_code_artifact_metadata(
        params.get("code_artifact_metadata"),
        workflow_yaml,
    )
    if code_artifact_metadata_error is not None:
        return {"ok": False, "error": code_artifact_metadata_error}
    if code_artifact_metadata:
        ctx.code_artifact_metadata = code_artifact_metadata
        ctx.workflow_verification_evidence.code_artifact_metadata = code_artifact_metadata
        params["code_artifact_metadata"] = code_artifact_metadata
    if allow_missing_credentials is None:
        allow_missing_credentials = getattr(ctx, "allow_untested_workflow_draft", False) is True
    if not allow_missing_credentials:
        credential_error = await _credential_reference_validation_error(workflow_yaml, ctx)
        if credential_error is not None:
            return {"ok": False, "error": credential_error}

    misbinding_findings = _credential_id_misbinding_findings(workflow_yaml)
    if misbinding_findings:
        LOG.info(
            "copilot credential id misbinding rejected",
            organization_id=ctx.organization_id,
            workflow_id=ctx.workflow_id,
            findings=misbinding_findings,
        )
        return {"ok": False, "error": _credential_id_misbinding_error_message(misbinding_findings)}

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

    challenge_http_error = _challenge_http_request_reject_message(ctx, workflow_yaml, ctx.workflow_yaml)
    if challenge_http_error:
        return {"ok": False, "error": challenge_http_error}

    # Post-emission reject of copilot-v2 writes that introduce a banned
    # block type. The schema pre_hook only fires when the LLM consults the
    # schema; this safety net fires regardless of emission path. Label-based
    # diff preserves legacy workflows — only NEW banned labels trip the reject.
    banned_items = _detect_new_banned_blocks(
        workflow_yaml,
        ctx.workflow_yaml,
        banned_types=_copilot_banned_block_types(ctx),
    )
    if banned_items:
        _record_banned_block_reject_span("_update_workflow", banned_items)
        return {"ok": False, "error": _banned_block_reject_message(banned_items, ctx)}

    composition_evidence_error = composition_page_evidence_error(ctx, workflow_yaml)
    if composition_evidence_error:
        LOG.info(
            "copilot composition page evidence rejected workflow",
            workflow_permanent_id=ctx.workflow_permanent_id,
            target_url=workflow_target_url(workflow_yaml),
        )
        return {"ok": False, "error": composition_evidence_error}

    try:
        workflow = _process_workflow_yaml(
            workflow_id=ctx.workflow_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
            workflow_yaml=workflow_yaml,
        )
        _record_workflow_proxy_location_span(workflow_yaml, workflow)

        # Param / top-level setting changes go through canonical because
        # prepare_workflow and the runtime parameter-row read consume canonical
        # values; terminal handlers roll back on non-auto-accept.
        prior_workflow = await _get_prior_workflow(ctx)
        requires_canonical_persist = _workflow_requires_canonical_persist(prior_workflow, workflow)
        if requires_canonical_persist:
            created_by_stamp = await resolve_copilot_created_by_stamp(ctx.workflow_id, ctx.organization_id)
            await app.WORKFLOW_SERVICE.update_workflow_definition(
                workflow_id=ctx.workflow_id,
                organization_id=ctx.organization_id,
                title=workflow.title,
                description=workflow.description,
                workflow_definition=workflow.workflow_definition,
                proxy_location=workflow.proxy_location,
                webhook_callback_url=workflow.webhook_callback_url,
                totp_verification_url=workflow.totp_verification_url,
                totp_identifier=workflow.totp_identifier,
                persist_browser_session=workflow.persist_browser_session,
                browser_profile_id=workflow.browser_profile_id,
                model=workflow.model,
                max_screenshot_scrolling_times=workflow.max_screenshot_scrolls,
                extra_http_headers=workflow.extra_http_headers,
                cdp_connect_headers=workflow.cdp_connect_headers,
                run_with=workflow.run_with,
                ai_fallback=workflow.ai_fallback,
                cache_key=workflow.cache_key,
                adaptive_caching=workflow.adaptive_caching,
                code_version=workflow.code_version,
                run_sequentially=workflow.run_sequentially,
                sequential_key=workflow.sequential_key,
                created_by=created_by_stamp,
                edited_by="copilot",
            )
            ctx.canonical_was_persisted_due_to_param_change = True
        ctx.staged_workflow_yaml = workflow_yaml
        ctx.staged_workflow = workflow
        ctx.has_staged_proposal = True
        ctx.workflow_yaml = workflow_yaml
        # Best-effort — narrative emit failures must never abort an
        # otherwise-successful update_workflow tool call. ``isinstance``
        # narrows the parameter's declared ``AgentContext`` to the
        # envelope-aware ``CopilotContext`` for mypy.
        if isinstance(ctx, CopilotContext) and ctx.stream is not None:
            try:
                await maybe_emit_design_end(ctx.stream, ctx)
                await emit_workflow_draft(ctx.stream, ctx, workflow)
            except Exception as emit_err:
                LOG.warning("copilot_narrative_workflow_draft_emit_failed", error=str(emit_err))
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


def _block_type_name(block: object) -> str:
    """Lowercase string name of a block's type, for both YAML and runtime blocks."""
    bt = getattr(block, "block_type", None)
    if bt is None:
        return ""
    name = getattr(bt, "value", None) or getattr(bt, "name", None) or str(bt)
    return str(name).lower()


def _blocks_by_label(workflow_definition: object | None) -> dict[str, object]:
    blocks = getattr(workflow_definition, "blocks", None) if workflow_definition else None
    by_label: dict[str, object] = {}
    if not blocks:
        return by_label
    for block in blocks:
        label = getattr(block, "label", None)
        if isinstance(label, str):
            by_label[label] = block
    return by_label


def _workflow_definition_block_labels(workflow_definition: object | None) -> list[str]:
    blocks = getattr(workflow_definition, "blocks", None) if workflow_definition else None
    labels: list[str] = []
    if not blocks:
        return labels
    for block in blocks:
        label = getattr(block, "label", None)
        if isinstance(label, str) and label:
            labels.append(label)
    return labels


def _current_workflow_block_labels(ctx: object) -> list[str]:
    workflow = getattr(ctx, "last_workflow", None)
    labels = _workflow_definition_block_labels(getattr(workflow, "workflow_definition", None))
    if labels:
        return labels
    workflow_yaml = getattr(ctx, "last_workflow_yaml", None)
    if not isinstance(workflow_yaml, str):
        return []
    blocks = _parse_workflow_blocks(workflow_yaml)
    if not blocks:
        return []
    yaml_labels: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            label = _block_label_from_yaml(block)
            if label:
                yaml_labels.append(label)
    return yaml_labels


def _current_workflow_has_evidence_block(ctx: object) -> bool:
    workflow = getattr(ctx, "last_workflow", None)
    blocks = getattr(getattr(workflow, "workflow_definition", None), "blocks", None)
    if blocks:
        return any(_block_type_name(block) in _OUTCOME_EVIDENCE_BLOCK_TYPES for block in blocks)
    workflow_yaml = getattr(ctx, "last_workflow_yaml", None)
    if not isinstance(workflow_yaml, str):
        return False
    return any(
        isinstance(block, dict) and _enum_or_string_name(block.get("block_type")) in _OUTCOME_EVIDENCE_BLOCK_TYPES
        for block in (_parse_workflow_blocks(workflow_yaml) or [])
    )


def _unverified_current_workflow_labels(ctx: object) -> list[str]:
    labels = _current_workflow_block_labels(ctx)
    verified = set(getattr(ctx, "verified_prefix_labels", []) or [])
    return [label for label in labels if label not in verified]


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
    old_definition: object | None,
    new_definition: object | None,
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


def _clear_runtime_anchor_evidence(copilot_ctx: Any) -> None:
    # Clears the runtime-anchor *trust* flags only. evidence.current_url /
    # page_title / workflow_run_id are left intact: an edit does not move the
    # browser, so they remain accurate observational context — the cleared flags
    # are what mark that state as no longer verified.
    evidence = copilot_ctx.workflow_verification_evidence
    copilot_ctx.verified_prefix_current_url = None
    evidence.live_page_state_verified = False
    evidence.verified_from_current_browser_state = False
    evidence.current_url_observed_after_workflow_run = False
    evidence.current_url_may_encode_runtime_state = False


def _reset_all_verified_trust(copilot_ctx: Any) -> None:
    evidence = copilot_ctx.workflow_verification_evidence
    copilot_ctx.verified_prefix_labels = []
    copilot_ctx.verified_block_outputs = {}
    copilot_ctx.last_full_workflow_test_ok = False
    evidence.block_verified = []
    evidence.full_workflow_verified = False
    _clear_runtime_anchor_evidence(copilot_ctx)


def _workflow_parameters_changed(prior_definition: object | None, new_definition: object | None) -> bool:
    new_by_key = {getattr(p, "key", None): p for p in (getattr(new_definition, "parameters", None) or [])}
    prior_by_key = {getattr(p, "key", None): p for p in (getattr(prior_definition, "parameters", None) or [])}
    # Any added or removed parameter is a change: a block may reference a key by
    # template without a config edit, so an added/removed key can alter behavior
    # the block-diff alone won't catch. Pure reordering is ignored (keyed access).
    if set(prior_by_key) != set(new_by_key):
        return True
    for key, prior_param in prior_by_key.items():
        try:
            if _stable_parameter_fingerprint(prior_param) != _stable_parameter_fingerprint(new_by_key[key]):
                return True
        except Exception:
            LOG.debug("Parameter fingerprint comparison failed on edit", exc_info=True)
            return True
    return False


def _invalidate_verified_state_on_edit(
    copilot_ctx: Any,
    prior_definition: object | None,
    new_definition: object | None,
) -> None:
    evidence = copilot_ctx.workflow_verification_evidence
    if new_definition is None:
        # An unknown new definition can't be reconciled against; fail closed.
        if copilot_ctx.verified_prefix_labels or evidence.block_verified:
            _reset_all_verified_trust(copilot_ctx)
        return
    prior_labels = _workflow_definition_block_labels(prior_definition)
    trusted = set(copilot_ctx.verified_prefix_labels or []) | set(evidence.block_verified or [])

    invalidated: set[str] = set()
    if trusted:
        # No reconcilable prior, or a parameter change that could alter any
        # block's behavior — fail closed rather than reuse unproven trust.
        if prior_definition is None or _workflow_parameters_changed(prior_definition, new_definition):
            _reset_all_verified_trust(copilot_ctx)
            return
        # Diff the full prior chain, not just trusted labels, so a change to an
        # unverified upstream block still propagates to downstream trusted ones.
        full_order = list(prior_labels)
        for label in list(copilot_ctx.verified_prefix_labels or []) + list(evidence.block_verified or []):
            if label not in full_order:
                full_order.append(label)
        try:
            invalidated = _find_invalidated_labels(prior_definition, new_definition, full_order) & trusted
        except Exception:
            LOG.debug("Verified-state invalidation diff failed on edit", exc_info=True)
            _reset_all_verified_trust(copilot_ctx)
            return

    if invalidated:
        copilot_ctx.verified_prefix_labels = [
            label for label in copilot_ctx.verified_prefix_labels if label not in invalidated
        ]
        for label in invalidated:
            copilot_ctx.verified_block_outputs.pop(label, None)
        evidence.block_verified = [label for label in evidence.block_verified if label not in invalidated]
        # The recorded prefix-end URL came from a run that included a now-invalid
        # block, so it is no longer a safe runtime anchor for a re-run.
        _clear_runtime_anchor_evidence(copilot_ctx)

    # The kept prefix must still be a contiguous leading run of the new workflow;
    # a reorder or upstream insertion breaks that (set-membership trust is
    # order-blind), so fail closed when it no longer is.
    current_labels = _workflow_definition_block_labels(new_definition)
    remaining = list(copilot_ctx.verified_prefix_labels or [])
    if remaining and remaining != current_labels[: len(remaining)]:
        _reset_all_verified_trust(copilot_ctx)
        return

    # The end-to-end claim survives only an identical block list whose every block
    # is still verified, so append/removal/reorder/config edits all drop it.
    verified = set(remaining) | set(evidence.block_verified or [])
    if not (current_labels == prior_labels and all(label in verified for label in current_labels)):
        evidence.full_workflow_verified = False
        copilot_ctx.last_full_workflow_test_ok = False


def _nearest_upstream_state_establisher(
    requested_labels: list[str], target_label: str, new_definition: object | None
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


def _block_can_start_browser_run(block: object) -> bool:
    if _block_type_name(block) == BlockType.GOTO_URL.value:
        return True
    return _valid_runtime_anchor_url(getattr(block, "url", None)) is not None


def _nearest_upstream_runnable_anchor(
    workflow_labels: list[str], target_label: str, new_definition: object | None
) -> str | None:
    by_label = _blocks_by_label(new_definition)
    try:
        idx = workflow_labels.index(target_label)
    except ValueError:
        return None
    for candidate in reversed(workflow_labels[:idx]):
        block = by_label.get(candidate)
        if block is not None and _block_can_start_browser_run(block):
            return candidate
    return workflow_labels[0] if workflow_labels[:idx] else None


def _serialized_frontier_block_configs(frontier_labels: list[str], new_definition: object | None) -> list[str]:
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


def _workflow_parameter_keys(definition: object | None) -> set[str]:
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
    new_definition: object | None,
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
    new_definition: object | None,
    serialized_configs: list[str] | None = None,
) -> set[str]:
    suffix_refs, block_form_refs, _ = _classify_frontier_jinja_refs(frontier_labels, new_definition, serialized_configs)
    return suffix_refs | block_form_refs


def _block_form_output_labels(
    frontier_labels: list[str],
    new_definition: object | None,
    serialized_configs: list[str] | None = None,
) -> set[str]:
    _, block_form_refs, _ = _classify_frontier_jinja_refs(frontier_labels, new_definition, serialized_configs)
    return block_form_refs


def _unknown_jinja_roots(
    frontier_labels: list[str],
    new_definition: object | None,
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


async def _get_prior_workflow_definition(ctx: AgentContext) -> object | None:
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


async def _get_prior_workflow(ctx: AgentContext) -> Workflow | None:
    """Return the prior Workflow; in-memory > re-parsed yaml > DB."""
    last_workflow = ctx.last_workflow
    if last_workflow is not None:
        return last_workflow
    last_yaml = ctx.last_workflow_yaml
    if last_yaml:
        try:
            return _process_workflow_yaml(
                workflow_id=ctx.workflow_id,
                workflow_permanent_id=ctx.workflow_permanent_id,
                organization_id=ctx.organization_id,
                workflow_yaml=last_yaml,
            )
        except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException):
            pass
    try:
        return await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
        )
    except Exception:
        LOG.warning(
            "Failed to fetch prior workflow for staging comparison; staging may skip a needed canonical write",
            exc_info=True,
        )
    return None


# Must stay in lockstep with the writers calling update_workflow_definition;
# missing fields silently drop accepted settings on auto-accept.
_CANONICAL_WORKFLOW_SETTING_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "proxy_location",
    "webhook_callback_url",
    "totp_verification_url",
    "totp_identifier",
    "persist_browser_session",
    "browser_profile_id",
    "model",
    "max_screenshot_scrolls",
    "extra_http_headers",
    "cdp_connect_headers",
    "run_with",
    "ai_fallback",
    "cache_key",
    "adaptive_caching",
    "code_version",
    "run_sequentially",
    "sequential_key",
)

# convert_workflow_definition regenerates ids/timestamps per call; ignore
# them when comparing parameters to react only to user intent.
_PARAMETER_FINGERPRINT_VOLATILE_KEYS = frozenset(
    {
        "workflow_parameter_id",
        "output_parameter_id",
        "aws_secret_parameter_id",
        "azure_secret_parameter_id",
        "azure_vault_credential_parameter_id",
        "bitwarden_credit_card_data_parameter_id",
        "bitwarden_login_credential_parameter_id",
        "bitwarden_sensitive_information_parameter_id",
        "credential_parameter_id",
        "onepassword_credential_parameter_id",
        "created_at",
        "modified_at",
    }
)


def _stable_parameter_fingerprint(parameter: Parameter) -> dict[str, Any]:
    dump = parameter.model_dump(mode="json")
    return {k: v for k, v in dump.items() if k not in _PARAMETER_FINGERPRINT_VOLATILE_KEYS}


def _workflow_requires_canonical_persist(prior: Workflow | None, new: Workflow) -> bool:
    if prior is None:
        return False
    for field_name in _CANONICAL_WORKFLOW_SETTING_FIELDS:
        if getattr(prior, field_name, None) != getattr(new, field_name, None):
            return True
    prior_params = prior.workflow_definition.parameters
    new_params = new.workflow_definition.parameters
    if len(prior_params) != len(new_params):
        return True
    prior_fingerprints = [_stable_parameter_fingerprint(p) for p in prior_params]
    new_fingerprints = [_stable_parameter_fingerprint(p) for p in new_params]
    return prior_fingerprints != new_fingerprints


def _plan_frontier(
    ctx: AgentContext,
    requested_labels: list[str],
    old_definition: object | None,
    new_definition: object | None,
) -> tuple[list[str], dict[str, Any], str | None]:
    """Plan the frontier execution.

    Returns ``(labels_to_execute, block_outputs_to_seed, frontier_start_label)``.

    Falls back to the full requested list on any ambiguity. When there is no
    workflow change (plain run path) the frontier is the first requested label,
    and we seed verified outputs referenced by the suffix plus prior
    browser-state outputs needed to start a downstream frontier.
    """
    if not requested_labels:
        return requested_labels, {}, None
    if new_definition is None:
        return requested_labels, {}, requested_labels[0]

    verified_outputs: dict[str, Any] = dict(ctx.verified_block_outputs or {})
    verified_prefix: list[str] = list(ctx.verified_prefix_labels or [])
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
        # No invalidation at all — unchanged request. Continue from the
        # first unverified requested label so a model may keep passing the
        # complete chain while the tool advances the browser in small
        # verified frontiers.
        next_frontier = _first_unverified_requested_label(requested_labels, verified_prefix_set)
        if next_frontier is not None:
            return _seed_for_frontier(requested_labels, next_frontier, verified_outputs, new_definition)

        # If the model accidentally asks to rerun an already-verified prefix,
        # keep the browser moving forward instead of spending another tool call
        # on work the current session has already covered.
        workflow_labels = _workflow_definition_block_labels(new_definition)
        next_workflow_frontier = _first_unverified_requested_label(workflow_labels, verified_prefix_set)
        if next_workflow_frontier is not None:
            frontier_idx = workflow_labels.index(next_workflow_frontier)
            return _seed_for_frontier(
                workflow_labels[: frontier_idx + 1],
                next_workflow_frontier,
                verified_outputs,
                new_definition,
            )

        return _seed_for_frontier(requested_labels, requested_labels[0], verified_outputs, new_definition)

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
        workflow_labels = _workflow_definition_block_labels(new_definition)
        if earliest in workflow_labels:
            workflow_prefix = workflow_labels[: workflow_labels.index(earliest)]
            if not all(label in verified_prefix_set for label in workflow_prefix):
                anchor = _nearest_upstream_runnable_anchor(workflow_labels, earliest, new_definition)
                if anchor is not None:
                    return _seed_for_frontier(
                        workflow_labels[workflow_labels.index(anchor) : workflow_labels.index(earliest) + 1],
                        anchor,
                        verified_outputs,
                        new_definition,
                    )
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


def _first_unverified_requested_label(requested_labels: list[str], verified_prefix_set: set[str]) -> str | None:
    for label in requested_labels:
        if label not in verified_prefix_set:
            return label
    return None


def _seed_for_frontier(
    requested_labels: list[str],
    frontier: str,
    verified_outputs: dict[str, Any],
    new_definition: object | None,
) -> tuple[list[str], dict[str, Any], str]:
    try:
        idx = requested_labels.index(frontier)
    except ValueError:
        return requested_labels, {}, requested_labels[0]
    labels_to_execute = requested_labels[idx:]
    workflow_labels = _workflow_definition_block_labels(new_definition)
    if frontier in workflow_labels:
        prefix_labels = workflow_labels[: workflow_labels.index(frontier)]
    else:
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
    by_label = _blocks_by_label(new_definition)
    for label in prefix_labels:
        block = by_label.get(label)
        if block is None or _block_type_name(block) not in _BLOCK_TYPES_STATE_ESTABLISHER:
            continue
        if label in verified_outputs:
            seed.setdefault(label, verified_outputs[label])
    return labels_to_execute, seed, frontier


_MAX_INCREMENTAL_PAGE_FRONTIER_LABELS = 2
_PAGE_CHANGING_FRONTIER_BLOCK_TYPES: frozenset[str] = frozenset(
    {
        BlockType.ACTION.value,
        BlockType.FILE_DOWNLOAD.value,
        BlockType.FILE_UPLOAD.value,
        BlockType.LOGIN.value,
        BlockType.NAVIGATION.value,
    }
)


def _frontier_block_type_names(labels: list[str], workflow_definition: object | None) -> list[str]:
    by_label = _blocks_by_label(workflow_definition)
    type_names: list[str] = []
    for label in labels:
        block = by_label.get(label)
        if block is None:
            continue
        type_name = _block_type_name(block)
        if type_name:
            type_names.append(type_name)
    return type_names


def _frontier_has_several_page_changing_stages(labels: list[str], workflow_definition: object | None) -> bool:
    type_names = _frontier_block_type_names(labels, workflow_definition)
    if len(type_names) <= _MAX_INCREMENTAL_PAGE_FRONTIER_LABELS:
        return False
    page_changing_count = sum(1 for type_name in type_names if type_name in _PAGE_CHANGING_FRONTIER_BLOCK_TYPES)
    return page_changing_count >= 2 or (page_changing_count >= 1 and len(type_names) >= 4)


def _frontier_includes_required_runtime_anchor(block_labels: list[str], labels_to_execute: list[str]) -> bool:
    if not block_labels or len(labels_to_execute) <= len(block_labels):
        return False
    return labels_to_execute[-len(block_labels) :] == block_labels


def _frontier_run_size_error(
    copilot_ctx: object,
    block_labels: list[str],
    labels_to_execute: list[str],
    workflow_definition: object | None,
) -> str | None:
    if len(labels_to_execute) <= _MAX_INCREMENTAL_PAGE_FRONTIER_LABELS:
        return None
    if _frontier_includes_required_runtime_anchor(block_labels, labels_to_execute):
        return None
    if getattr(copilot_ctx, "build_phase", None) not in (BuildPhase.COMPOSING, BuildPhase.TESTING):
        return None
    if not _frontier_has_several_page_changing_stages(labels_to_execute, workflow_definition):
        return None

    suggested = labels_to_execute[:_MAX_INCREMENTAL_PAGE_FRONTIER_LABELS]
    remaining = labels_to_execute[_MAX_INCREMENTAL_PAGE_FRONTIER_LABELS:]
    return (
        "Workflow validation failed: this browser test frontier is too long for a multi-stage "
        "page-changing workflow. Keep the same complete workflow YAML, but shrink only the "
        f"block_labels argument to the next 1-2 unverified labels: {suggested!r}. "
        "If a prior run already advanced the browser, inspect that reached page "
        '(inspect_page_for_composition(target_url="current_page")) to ground the next labels in '
        "what is actually there rather than shrinking the frontier blind. "
        f"Do not remove later blocks from the YAML; test them after this frontier succeeds. "
        f"Deferred labels: {remaining!r}. Requested labels: {block_labels!r}."
    )


# Watchdog exit reasons. ``success`` means the run reached a trustworthy
# terminal status inside the poll loop OR after the post-drain reconcile.
# The three non-success reasons share the reconcile path but produce distinct
# error messages: ``stagnation`` is the primary trip (no progress signals
# for ``RUN_BLOCKS_STAGNATION_WINDOW_SECONDS`` seconds), ``ceiling`` is the
# last-resort budget-exhausted branch, and ``task_exit_unfinalized`` is the
# rare race where ``execute_workflow`` naturally exits before writing a
# terminal row.
WatchdogExitReason = Literal[
    "success",
    "stagnation",
    "ceiling",
    "per_tool_budget",
    "task_exit_unfinalized",
    "active_run_terminal_evidence",
]


def _watchdog_exit_allows_terminal_promotion(exit_reason: WatchdogExitReason | None) -> bool:
    return exit_reason != "active_run_terminal_evidence"


# Block types that legitimately execute long silent periods: one DB write on
# entry, work done without intermediate writes (sleep / LLM call / await human
# input / browser download wait), one write on finish. The watchdog can't
# distinguish these from "stuck", so any invocation that includes one disables
# stagnation for the whole run and relies on the safety ceiling alone.
_QUIET_BLOCK_TYPES: frozenset[str] = frozenset(
    {
        BlockType.WAIT.value,
        BlockType.TEXT_PROMPT.value,
        BlockType.HUMAN_INTERACTION.value,
        BlockType.FILE_DOWNLOAD.value,
    }
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
            f"mid-execution and may have left partial side effects). If the result "
            f"includes a current_url, inspect that current page before any further "
            f'block-running call with inspect_page_for_composition(target_url="current_page"). '
            f"Generic screenshot/evaluate reads can help answer the user, but they do not "
            f"satisfy the bounded page-evidence contract for workflow mutations. Use the "
            f"bounded evidence to decide whether the answer is already visible, whether "
            f"a challenge-gated submit/search control is still disabled, or which page-state "
            f"change is still missing. If challenge_state.gates_submit_controls=true and "
            f"the requested answer is not visible, stop and report the observed anti-bot "
            f"blocker instead of retrying the same solve/wait/submit chain. Only then call "
            f"update_and_run_blocks with a smaller chain — the first 1-2 unverified blocks. "
            f"Verified-prefix state is preserved, so the next call only re-runs from the new frontier. "
            f"Do NOT retry the same chain unchanged — a longer "
            f"run won't fit either."
        )
        current_url, _ = await _fallback_page_info(ctx)
        if current_url:
            message += f" Browser was on: {current_url}"
        return message
    elif exit_reason == "active_run_terminal_evidence":
        message = (
            "The active run was interrupted because bounded current-page evidence matched the requested "
            "browser terminal state while the workflow run was still in progress.\n"
            f"Run ID: {workflow_run_id}.\n"
            "This is NOT full workflow verification: the requested browser state was observed, but the durable "
            "workflow chain still needs diagnosis/repair and a clean verification run. Next step: call "
            "get_run_results with this workflow_run_id to inspect the active run boundary, preserve the observed "
            "current-page evidence, and update only the block(s) that overshot or kept running after the state "
            "was reached. Do NOT report end-to-end success unless a corrected workflow run verifies cleanly."
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


def _watchdog_user_failure_reason(
    exit_reason: WatchdogExitReason,
    workflow_run_id: str,
    budget_seconds: int,
    run: WorkflowRun | None,
) -> str:
    if exit_reason == "stagnation":
        body = f"The run stopped after no observable progress for {RUN_BLOCKS_STAGNATION_WINDOW_SECONDS}s."
    elif exit_reason == "per_tool_budget":
        body = f"The run exceeded the {budget_seconds}s per-tool-call budget while still making progress."
    elif exit_reason == "active_run_terminal_evidence":
        body = (
            "The active run reached the requested browser state before the workflow finished, "
            "so it was interrupted for diagnosis/repair. Full workflow verification is still required."
        )
    elif exit_reason == "ceiling":
        body = f"The run exceeded the {budget_seconds}s absolute ceiling while still showing progress."
    else:
        status = f" Last observed status: {run.status}." if run is not None else ""
        body = "The run ended before recording a trustworthy terminal status." + status
    return f"{body} Run ID: {workflow_run_id}. Outcome is uncertain."


def _watchdog_user_facing_summary(
    exit_reason: WatchdogExitReason,
    budget_seconds: int,
    run: WorkflowRun | None,
) -> str:
    if exit_reason == "stagnation":
        return f"The run stopped after no observable progress for {RUN_BLOCKS_STAGNATION_WINDOW_SECONDS}s."
    if exit_reason == "per_tool_budget":
        return f"The run exceeded the {budget_seconds}s per-tool-call budget while still making progress."
    if exit_reason == "ceiling":
        return f"The run exceeded the {budget_seconds}s absolute ceiling while still showing progress."
    if run is not None:
        return f"The run ended before recording a trustworthy terminal status. Last observed status: {run.status}."
    return "The run ended before recording a trustworthy terminal status."


def _workflow_with_runtime_block_goal_context(workflow: Workflow, ctx: CopilotContext) -> Workflow:
    block_goal_main_goal = ctx.block_goal_main_goal or ctx.user_message or ""
    if not block_goal_main_goal:
        LOG.warning("run_blocks invoked without block-goal context; using persisted workflow goals unchanged")
        return workflow
    return wrap_workflow_block_goals(workflow, block_goal_main_goal)


def _valid_runtime_anchor_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or url in {"about:blank", ":"}:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _blank_runtime_page_url(value: object) -> bool:
    if not isinstance(value, str):
        return True
    # Chrome/CDP can expose ":" while the page is still in early blank-page initialization.
    return value.strip() in {"about:blank", "", ":"}


def _missing_runtime_frontier_block_url(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _same_runtime_page(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        left_parsed = urlparse(left)
        right_parsed = urlparse(right)
    except ValueError:
        return False
    return (
        left_parsed.scheme.lower(),
        left_parsed.netloc.lower(),
        left_parsed.path,
        left_parsed.query,
    ) == (
        right_parsed.scheme.lower(),
        right_parsed.netloc.lower(),
        right_parsed.path,
        right_parsed.query,
    )


def _frontier_anchor_url_from_value(value: object, *, depth: int = 0) -> str | None:
    if depth > 3:
        return None
    direct = _valid_runtime_anchor_url(value)
    if direct is not None:
        return direct
    if isinstance(value, dict):
        for key in ("current_url", "url", "page_url"):
            candidate = _valid_runtime_anchor_url(value.get(key))
            if candidate is not None:
                return candidate
        for nested in value.values():
            candidate = _frontier_anchor_url_from_value(nested, depth=depth + 1)
            if candidate is not None:
                return candidate
    if isinstance(value, list):
        for nested in value:
            candidate = _frontier_anchor_url_from_value(nested, depth=depth + 1)
            if candidate is not None:
                return candidate
    return None


def _frontier_runtime_anchor_url(ctx: CopilotContext, block_outputs_to_seed: dict[str, Any]) -> str | None:
    for value in (
        ctx.verified_prefix_current_url,
        getattr(ctx, "composition_page_evidence", None),
    ):
        candidate = _frontier_anchor_url_from_value(value)
        if candidate is not None:
            return candidate
    for value in reversed(list((block_outputs_to_seed or {}).values())):
        candidate = _frontier_anchor_url_from_value(value)
        if candidate is not None:
            return candidate
    return None


def _iter_workflow_model_blocks(blocks: list[BlockTypeVar] | None) -> list[BlockTypeVar]:
    if not isinstance(blocks, list):
        return []
    return get_all_blocks(blocks)


def _workflow_model_block_by_label(workflow_definition: object | None, label: str | None) -> BlockTypeVar | None:
    if workflow_definition is None or not label:
        return None
    blocks = workflow_definition.blocks if hasattr(workflow_definition, "blocks") else None
    for block in _iter_workflow_model_blocks(blocks):
        if block.label == label:
            return block
    return None


def _has_verified_prefix_before_frontier(
    ctx: CopilotContext, workflow_definition: object | None, frontier_label: str | None
) -> bool:
    if not frontier_label:
        return False
    workflow_labels = _workflow_definition_block_labels(workflow_definition)
    if frontier_label not in workflow_labels:
        return False
    prefix_labels = workflow_labels[: workflow_labels.index(frontier_label)]
    if not prefix_labels:
        return False
    verified = set(ctx.verified_prefix_labels or [])
    return all(label in verified for label in prefix_labels)


def _workflow_with_runtime_frontier_anchor(
    workflow: Workflow,
    ctx: CopilotContext,
    *,
    labels_to_execute: list[str],
    frontier_start_label: str | None,
    block_outputs_to_seed: dict[str, Any],
) -> tuple[Workflow, str | None]:
    if not labels_to_execute:
        return workflow, None
    workflow_definition = workflow.workflow_definition
    if not _has_verified_prefix_before_frontier(ctx, workflow_definition, frontier_start_label):
        return workflow, None

    first_label = labels_to_execute[0]
    first_block = _workflow_model_block_by_label(workflow_definition, first_label)
    if first_block is None or not hasattr(first_block, "url"):
        return workflow, None

    anchor_url = _frontier_runtime_anchor_url(ctx, block_outputs_to_seed)
    if anchor_url is None:
        return workflow, None

    existing_url = _valid_runtime_anchor_url(first_block.url if hasattr(first_block, "url") else None)
    if existing_url is not None and not _same_runtime_page(existing_url, anchor_url):
        return workflow, None

    if existing_url is not None:
        if _block_type_name(first_block) != BlockType.NAVIGATION.value:
            return workflow, None
        anchored = workflow.model_copy(deep=True)
        anchored_block = _workflow_model_block_by_label(anchored.workflow_definition, first_label)
        if anchored_block is None or not hasattr(anchored_block, "url"):
            return workflow, None
        anchored_block.url = None
        LOG.info(
            "Cleared runtime frontier URL to preserve browser state",
            frontier_start_label=frontier_start_label,
            first_block_label=first_label,
            existing_url=existing_url,
            continuation_url=anchor_url,
        )
        return anchored, anchor_url

    LOG.info(
        "Preserved runtime frontier browser state without URL reload",
        frontier_start_label=frontier_start_label,
        first_block_label=first_label,
        continuation_url=anchor_url,
    )
    return workflow, anchor_url


async def _workflow_with_runtime_frontier_starter_url_seed(
    workflow: Workflow,
    ctx: CopilotContext,
    *,
    labels_to_execute: list[str],
    runtime_frontier_anchor_url: str | None,
) -> Workflow:
    if not labels_to_execute or runtime_frontier_anchor_url is None or not ctx.browser_session_id:
        return workflow

    first_label = labels_to_execute[0]
    first_block = _workflow_model_block_by_label(workflow.workflow_definition, first_label)
    if (
        first_block is None
        or not hasattr(first_block, "url")
        or _block_type_name(first_block) != BlockType.NAVIGATION.value
        or not _missing_runtime_frontier_block_url(first_block.url)
    ):
        return workflow

    current_page_url: str | None = None
    try:
        browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
            session_id=ctx.browser_session_id,
            organization_id=ctx.organization_id,
        )
        if browser_state is not None:
            page = await browser_state.get_working_page()
            # Playwright Page.url is exposed as a dynamic property at this boundary.
            current_page_url = page.url if page is not None else None
    except Exception:
        LOG.debug(
            "Failed to inspect runtime frontier browser page before starter URL seed",
            browser_session_id=ctx.browser_session_id,
            frontier_start_label=first_label,
            exc_info=True,
        )

    if not _blank_runtime_page_url(current_page_url):
        LOG.info(
            "Preserved attached runtime frontier browser page",
            browser_session_id=ctx.browser_session_id,
            frontier_start_label=first_label,
            current_url=current_page_url,
            continuation_url=runtime_frontier_anchor_url,
        )
        return workflow

    seeded = workflow.model_copy(deep=True)
    seeded_block = _workflow_model_block_by_label(seeded.workflow_definition, first_label)
    # Defensive: the copied workflow definition should preserve labels and URL fields.
    if seeded_block is None or not hasattr(seeded_block, "url"):
        return workflow
    seeded_block.url = runtime_frontier_anchor_url
    LOG.info(
        "Seeded runtime frontier starter URL because attached browser page was blank",
        browser_session_id=ctx.browser_session_id,
        frontier_start_label=first_label,
        current_url=current_page_url,
        continuation_url=runtime_frontier_anchor_url,
    )
    return seeded


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

    # Common-case staging leaves the canonical row stale; prefer the staged copy.
    workflow = ctx.staged_workflow
    if workflow is None:
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
    runtime_workflow = _workflow_with_runtime_block_goal_context(workflow, ctx)
    runtime_workflow, runtime_frontier_anchor_url = _workflow_with_runtime_frontier_anchor(
        runtime_workflow,
        ctx,
        labels_to_execute=labels_to_execute,
        frontier_start_label=frontier_start_label,
        block_outputs_to_seed=block_outputs_to_seed,
    )
    runtime_frontier_starter_url_seeded = False

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

    seeded_runtime_workflow = await _workflow_with_runtime_frontier_starter_url_seed(
        runtime_workflow,
        ctx,
        labels_to_execute=labels_to_execute,
        runtime_frontier_anchor_url=runtime_frontier_anchor_url,
    )
    runtime_frontier_starter_url_seeded = seeded_runtime_workflow is not runtime_workflow
    runtime_workflow = seeded_runtime_workflow

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
            workflow_override=runtime_workflow,
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
    active_run_terminal_evidence: ActiveRunTerminalEvidenceSample | None = None
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
    last_block_fetch_monotonic = 0.0
    active_terminal_monitor_enabled = await _active_run_terminal_monitor_enabled(ctx)
    next_active_terminal_monitor_monotonic = (
        started_monotonic + _ACTIVE_RUN_TERMINAL_MONITOR_INITIAL_DELAY_SECONDS
        if active_terminal_monitor_enabled
        else float("inf")
    )
    active_terminal_monitor_samples = 0

    try:
        while True:
            await asyncio.sleep(RUN_BLOCKS_POLL_INTERVAL_SECONDS)

            run, step_ts, block_ts = await _read_progress_sources(ctx, workflow_run.workflow_run_id)

            if narrator_enabled:
                assert narrator_state is not None  # narrator_enabled implies non-None
                tick_result = await narrator_poll_tick(
                    narrator_state,
                    current_block_ts=block_ts,
                    prior_block_ts=prior_block_ts,
                    last_block_fetch_monotonic=last_block_fetch_monotonic,
                    seen_block_states=seen_block_states,
                    fetch_block_statuses=lambda: app.DATABASE.observer.get_workflow_run_blocks(
                        workflow_run_id=workflow_run.workflow_run_id,
                        organization_id=ctx.organization_id,
                    ),
                    stream=ctx.stream,
                    block_state_map=ctx.block_state_map,
                    block_started_at_map=ctx.block_started_at_map,
                    block_ended_at_map=ctx.block_ended_at_map,
                )
                prior_block_ts = tick_result.prior_block_ts
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

            if (
                active_terminal_monitor_enabled
                and not is_paused
                and active_terminal_monitor_samples < _ACTIVE_RUN_TERMINAL_MONITOR_MAX_SAMPLES
                and now >= next_active_terminal_monitor_monotonic
            ):
                active_terminal_monitor_samples += 1
                next_active_terminal_monitor_monotonic += _ACTIVE_RUN_TERMINAL_MONITOR_INTERVAL_SECONDS
                sample = await _active_run_terminal_evidence_sample(
                    ctx,
                    workflow_run_id=workflow_run.workflow_run_id,
                    labels_to_execute=labels_to_execute,
                    sample_index=active_terminal_monitor_samples,
                )
                if sample is not None:
                    post_sample_run = await _safe_read_workflow_run(
                        workflow_run.workflow_run_id,
                        ctx.organization_id,
                        context="active-terminal-post-sample",
                    )
                    if post_sample_run is not None:
                        run = post_sample_run
                    active_run_terminal_evidence = sample
                    exit_reason = "active_run_terminal_evidence"
                    break

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
            if (
                _watchdog_exit_allows_terminal_promotion(exit_reason)
                and pre_cancel_run is not None
                and WorkflowRunStatus(pre_cancel_run.status).is_final()
            ):
                final_status = pre_cancel_run.status
                run = pre_cancel_run
                exit_reason = "success"
            else:
                if pre_cancel_run is not None:
                    run = pre_cancel_run
                if run is None or not WorkflowRunStatus(run.status).is_final():
                    await _cancel_run_task_if_not_final(run_task, workflow_run.workflow_run_id)
                    run_cancelled_by_watchdog = True
                    run = await _safe_read_workflow_run(
                        workflow_run.workflow_run_id, ctx.organization_id, context="post-drain"
                    )
                if _watchdog_exit_allows_terminal_promotion(exit_reason):
                    trusted = _trusted_post_drain_status(run)
                    if trusted is not None:
                        final_status = trusted
                        exit_reason = "success"

        if exit_reason != "success":
            assert exit_reason is not None  # narrows for mypy; outer check excludes "success" but not None
            _mark_pending_reconciliation_run(ctx, workflow_run.workflow_run_id)
            error_msg = await _watchdog_error_message(
                exit_reason, ctx, workflow_run.workflow_run_id, run, budget_seconds
            )
            user_failure_reason = _watchdog_user_failure_reason(
                exit_reason, workflow_run.workflow_run_id, budget_seconds, run
            )
            user_facing_summary = _watchdog_user_facing_summary(exit_reason, budget_seconds, run)
            current_url, page_title = await _fallback_page_info(ctx)
            if exit_reason == "active_run_terminal_evidence" and active_run_terminal_evidence is not None:
                result: dict[str, Any] = _active_run_terminal_evidence_result(
                    workflow_run_id=workflow_run.workflow_run_id,
                    run_status=run.status if run is not None else None,
                    sample=active_run_terminal_evidence,
                    requested_block_labels=list(block_labels),
                    executed_block_labels=list(labels_to_execute),
                    current_url=current_url,
                    page_title=page_title,
                )
                result["error"] = error_msg
                result["data"]["failure_reason"] = user_failure_reason
            else:
                result = {
                    "ok": False,
                    "error": error_msg,
                    "data": {
                        "workflow_run_id": workflow_run.workflow_run_id,
                        "overall_status": run.status if run is not None else None,
                        "failure_reason": user_failure_reason,
                        "current_url": current_url,
                        "page_title": page_title,
                    },
                }
            result["data"]["control_signal"] = {
                "kind": f"watchdog_{exit_reason}",
                "user_facing_summary": user_facing_summary,
            }
            result["data"]["user_facing_summary"] = user_facing_summary
            if exit_reason == "per_tool_budget":
                # Stable failure_categories entry so consecutive budget trips
                # hash to the same streak signature; without it the run_id in
                # ``error_msg`` would make every trip unique.
                result["data"]["failure_categories"] = [
                    {
                        "category": PER_TOOL_BUDGET_FAILURE_CATEGORY,
                        "confidence_float": 1.0,
                        "reasoning": (
                            f"Per-tool-call budget of {budget_seconds}s exceeded; "
                            "the run was making progress but cannot fit in a single tool call."
                        ),
                    }
                ]
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
    if runtime_frontier_anchor_url is not None:
        result_data["runtime_frontier_anchor_url"] = runtime_frontier_anchor_url
    if runtime_frontier_starter_url_seeded:
        result_data["runtime_frontier_starter_url_seeded"] = True
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
        verified_current_url = _valid_runtime_anchor_url(current_url)
        if verified_current_url is not None:
            ctx.verified_prefix_current_url = verified_current_url

    return build_run_blocks_response(run_ok, result_data)


async def _get_run_results(params: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
    workflow_run_id = params.get("workflow_run_id")
    pending_run_id = getattr(ctx, "pending_reconciliation_run_id", None)
    if isinstance(pending_run_id, str) and pending_run_id:
        if workflow_run_id and workflow_run_id != pending_run_id:
            return {
                "ok": False,
                "error": (
                    f"Run inspection is pending for {pending_run_id}; "
                    "call get_run_results with that workflow_run_id first."
                ),
            }
        workflow_run_id = pending_run_id
    if not workflow_run_id:
        same_turn_run_id = getattr(ctx, "last_successful_run_blocks_workflow_run_id", None)
        if not isinstance(same_turn_run_id, str) or not same_turn_run_id:
            same_turn_run_id = getattr(ctx, "last_run_blocks_workflow_run_id", None)
        if isinstance(same_turn_run_id, str) and same_turn_run_id:
            workflow_run_id = same_turn_run_id

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
    if getattr(run, "workflow_permanent_id", None) != ctx.workflow_permanent_id:
        return {"ok": False, "error": f"Workflow run not found for this workflow: {workflow_run_id}"}

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
    current_url, page_title = await _fallback_page_info(ctx)
    if current_url:
        result_data["current_url"] = current_url
        result_data["page_title"] = page_title
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
        browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
            session_id=ctx.browser_session_id,
            organization_id=ctx.organization_id,
        )
        if not browser_state:
            return "", ""
        page = await browser_state.get_or_create_page()
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


def _bounded_observation_text(value: object, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


@dataclass(frozen=True)
class _ObservedFieldEvidence:
    name: str
    id: str
    label: str
    type: str
    placeholder: str
    required: bool
    selector: str

    @classmethod
    def from_raw(cls, raw_field: dict[str, Any]) -> _ObservedFieldEvidence:
        label = raw_field.get("label")
        if not label and isinstance(raw_field.get("labels"), list):
            label = " ".join(str(item) for item in raw_field["labels"][:2])
        return cls(
            name=_bounded_observation_text(raw_field.get("name"), 120),
            id=_bounded_observation_text(raw_field.get("id"), 120),
            label=_bounded_observation_text(label, 240),
            type=_bounded_observation_text(raw_field.get("type"), 40),
            placeholder=_bounded_observation_text(raw_field.get("placeholder"), 240),
            required=bool(raw_field.get("required")),
            selector=_bounded_observation_text(raw_field.get("selector"), 160),
        )

    def as_evidence(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "id": self.id,
            "label": self.label,
            "type": self.type,
            "placeholder": self.placeholder,
            "required": self.required,
            "selector": self.selector,
        }


def _normalize_observed_fields(raw_fields: object) -> list[dict[str, Any]]:
    if not isinstance(raw_fields, list):
        return []
    fields: list[dict[str, Any]] = []
    for raw_field in raw_fields[:20]:
        if not isinstance(raw_field, dict):
            continue
        fields.append(_ObservedFieldEvidence.from_raw(raw_field).as_evidence())
    return fields


def _normalize_observed_forms(observed_data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_forms = observed_data.get("forms")
    forms: list[dict[str, Any]] = []
    if isinstance(raw_forms, list):
        for raw_form in raw_forms[:5]:
            if not isinstance(raw_form, dict):
                continue
            fields = _normalize_observed_fields(raw_form.get("fields") or raw_form.get("inputs"))
            submit_controls = _normalize_observed_fields(
                raw_form.get("submit_controls") or raw_form.get("buttons") or raw_form.get("submits")
            )
            forms.append(
                {
                    "id": _bounded_observation_text(raw_form.get("id"), 120),
                    "name": _bounded_observation_text(raw_form.get("name"), 120),
                    "action": _bounded_observation_text(raw_form.get("action"), 240),
                    "method": _bounded_observation_text(raw_form.get("method"), 20),
                    "fields": fields,
                    "submit_controls": submit_controls[:10],
                }
            )
    if forms:
        return forms
    fields = _normalize_observed_fields(observed_data.get("inputs") or observed_data.get("fields"))
    if not fields:
        return []
    return [{"id": "", "name": "", "action": "", "method": "", "fields": fields, "submit_controls": []}]


def _evaluate_observed_payload(observed_data: object) -> dict[str, Any]:
    if not isinstance(observed_data, dict):
        return {}
    raw_result = observed_data.get("result")
    if isinstance(raw_result, dict):
        payload: dict[str, Any] = dict(raw_result)
    elif isinstance(raw_result, list):
        payload = {"rows": raw_result}
    else:
        payload = {}
    for key, value in observed_data.items():
        if key == "result":
            continue
        if key not in payload or payload.get(key) in (None, "", [], {}):
            payload[key] = value
    return payload or observed_data


def _observed_row_text(row: object) -> str:
    if isinstance(row, str):
        return _bounded_observation_text(row, 500)
    if not isinstance(row, dict):
        return ""
    parts: list[str] = []
    for key in ("text", "label", "name", "title"):
        value = row.get(key)
        if isinstance(value, str):
            parts.append(value)
    cells = row.get("cells")
    if isinstance(cells, list):
        for cell in cells[:12]:
            if isinstance(cell, dict):
                for key in ("text", "value"):
                    value = cell.get(key)
                    if isinstance(value, str):
                        parts.append(value)
            elif isinstance(cell, str):
                parts.append(cell)
    return _bounded_observation_text(" ".join(parts), 500)


def _normalize_observed_result_containers(observed_data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_results = observed_data.get("result_containers") or observed_data.get("tables")
    containers: list[dict[str, Any]] = []
    if isinstance(raw_results, list):
        for item in raw_results[:8]:
            if isinstance(item, dict):
                containers.append(
                    {
                        "tag": _bounded_observation_text(item.get("tag") or item.get("type"), 40),
                        "id": _bounded_observation_text(item.get("id"), 120),
                        "selector": _bounded_observation_text(item.get("selector"), 160),
                    }
                )
    if containers:
        return containers
    raw_rows = observed_data.get("rows")
    if isinstance(raw_rows, list) and raw_rows:
        sample_rows = [text for row in raw_rows[:5] if (text := _observed_row_text(row))]
        return [
            {
                "tag": "table",
                "id": _bounded_observation_text(observed_data.get("id"), 120),
                "selector": _bounded_observation_text(observed_data.get("selector"), 160),
                "row_count": len(raw_rows),
                "sample_rows": sample_rows,
            }
        ]
    table = observed_data.get("table")
    if table:
        return [{"tag": "table", "id": "", "selector": ""}]
    return []


def _normalize_evaluate_challenge_state(
    observed_data: dict[str, Any],
    anti_bot_indicators: list[str],
) -> dict[str, Any]:
    detected = bool(anti_bot_indicators)
    gated_controls: list[dict[str, Any]] = []
    for key, value in observed_data.items():
        normalized_key = str(key).strip().lower().replace("_", " ").replace("-", " ")
        if "disabled" not in normalized_key or value is not True:
            continue
        if any(token in normalized_key for token in ("submit", "search", "button", "btn")):
            gated_controls.append(
                {
                    "text": _bounded_observation_text(str(key), 120),
                    "id": "",
                    "name": _bounded_observation_text(str(key), 120),
                    "selector": "",
                    "disabled": True,
                }
            )
    indicator_text = " ".join(anti_bot_indicators).lower()
    if "access denied" in indicator_text:
        kind = "access_denied"
    elif "turnstile" in indicator_text or "captcha" in indicator_text or "are you a robot" in indicator_text:
        kind = "captcha"
    elif detected:
        kind = "human_verification"
    else:
        kind = "none"
    return {
        "detected": detected,
        "kind": kind,
        "source": "mcp_evaluate" if detected else "",
        "indicators": anti_bot_indicators[:8],
        "requires_human_verification": detected,
        "visual_location": "",
        "gates_submit_controls": bool(detected and gated_controls),
        "gated_submit_controls": gated_controls[:5] if detected else [],
    }


def _evaluate_anti_bot_indicators(observed_data: dict[str, Any], text: str) -> list[str]:
    key_text = " ".join(str(key) for key in observed_data.keys()).lower()
    combined = f"{text} {key_text}"
    return [pattern for pattern in _DISCOVERY_ANTI_BOT_PATTERNS if pattern in combined]


def _normalize_evaluate_page_schema(observed_data: object) -> dict[str, Any]:
    observed_data = _evaluate_observed_payload(observed_data)
    if not observed_data:
        return {}
    text_parts = [
        observed_data.get("body"),
        observed_data.get("bodyText"),
        observed_data.get("text"),
        observed_data.get("html"),
    ]
    text = " ".join(part for part in text_parts if isinstance(part, str)).lower()[:4096]
    anti_bot = _evaluate_anti_bot_indicators(observed_data, text)
    forms = _normalize_observed_forms(observed_data)
    result_containers = _normalize_observed_result_containers(observed_data)
    if not forms and not result_containers and not anti_bot:
        return {}
    return {
        "forms": forms,
        "result_containers": result_containers,
        "anti_bot_indicators": anti_bot,
        "challenge_state": _normalize_evaluate_challenge_state(observed_data, anti_bot),
        "evidence_sources": ["mcp_evaluate"],
        "evidence_confidence": (
            _EVALUATE_EVIDENCE_CONFIDENCE_WITH_SCHEMA
            if forms or result_containers
            else _EVALUATE_EVIDENCE_CONFIDENCE_ANTIBOT_ONLY
        ),
    }


def _record_composition_page_observation(
    ctx: AgentContext,
    *,
    source_tool: str,
    url: str,
    title: str = "",
    observed_data: object | None = None,
    append_to_flow: bool = False,
    reached_via: str = "current_page",
) -> int | None:
    if not url:
        return None
    _mark_post_run_page_observed(ctx, source_tool=source_tool, url=url)
    if title:
        _workflow_verification_evidence(ctx).page_title = title[:160]
    existing = ctx.composition_page_evidence

    evidence: dict[str, Any] = {
        "inspected_url": url,
        "current_url": url,
        "page_title": title[:240],
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "anti_bot_indicators": [],
        "evidence_confidence": 0.0,
        "source_tool": source_tool,
        "observed_after_workflow_run": False,
    }
    if isinstance(observed_data, dict):
        observed_title = observed_data.get("title")
        if isinstance(observed_title, str) and observed_title and not evidence["page_title"]:
            evidence["page_title"] = observed_title[:240]
    if source_tool == "get_browser_screenshot":
        evidence.update(
            {
                "evidence_sources": ["screenshot"],
                "screenshot_used": True,
            }
        )
    elif source_tool == "evaluate":
        evidence.update(_normalize_evaluate_page_schema(observed_data))
    run_id = ctx.last_run_blocks_workflow_run_id
    if isinstance(run_id, str) and run_id:
        evidence["workflow_run_id"] = run_id
        evidence["observed_after_workflow_run"] = True

    observation_step: int | None = None
    if append_to_flow and has_bounded_page_schema(evidence):
        actual_reached_via = reached_via
        if reached_via == "auto":
            if isinstance(run_id, str) and run_id:
                actual_reached_via = "post_run"
            elif _consume_pending_browser_interaction_observation(ctx, current_url=url, evidence=evidence):
                actual_reached_via = "interaction"
            else:
                actual_reached_via = "current_page"
        observation_step = _append_flow_evidence(ctx, evidence, reached_via=actual_reached_via)

    if not _should_keep_existing_composition_page_evidence(existing, evidence):
        ctx.composition_page_evidence = evidence
    return observation_step


def _composition_evidence_page_url(evidence: dict[str, Any] | None) -> str | None:
    if not isinstance(evidence, dict):
        return None
    for key in ("current_url", "inspected_url"):
        value = evidence.get(key)
        if isinstance(value, str) and value.strip() and value != "current_page":
            return value.strip()
    return None


def _should_keep_existing_composition_page_evidence(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> bool:
    if not isinstance(existing, dict) or existing.get("source_tool") != "inspect_page_for_composition":
        return False
    if incoming.get("source_tool") != "evaluate":
        return True
    if not has_bounded_page_schema(incoming):
        return True
    if not has_bounded_page_schema(existing):
        return False
    existing_url = _composition_evidence_page_url(existing)
    incoming_url = _composition_evidence_page_url(incoming)
    if existing_url and incoming_url and not _same_page_ignoring_fragment(existing_url, incoming_url):
        return False
    return True


def _completion_verification_criteria(copilot_ctx: Any) -> list[Any]:
    policy = getattr(copilot_ctx, "request_policy", None)
    # A method-mandated criterion asserts HOW the goal was reached; the outcome
    # judge sees only end-state evidence.
    return [c for c in (policy.completion_criteria if policy is not None else []) if not c.method_mandated]


def _verified_context_block_labels_for_snapshot(
    copilot_ctx: Any, current_labels: list[str], executed_labels: list[str]
) -> list[str]:
    current_label_set = set(current_labels)
    executed_label_set = set(executed_labels)
    candidates: set[str] = set()
    for raw_values in (
        getattr(copilot_ctx, "verified_prefix_labels", None),
        getattr(getattr(copilot_ctx, "workflow_verification_evidence", None), "block_verified", None),
    ):
        if not isinstance(raw_values, list):
            continue
        candidates.update(str(label) for label in raw_values if isinstance(label, str) and label in current_label_set)
    return [label for label in current_labels if label in candidates and label not in executed_label_set]


def _build_page_observation_evidence_snapshot(
    copilot_ctx: Any,
    *,
    url: str,
    title: str = "",
    observed_data: object | None = None,
) -> RunEvidenceSnapshot:
    run_id = getattr(copilot_ctx, "last_run_blocks_workflow_run_id", None)
    block_outputs: dict[str, Any] = {}
    if isinstance(observed_data, dict) and observed_data:
        block_outputs["current_page_observation"] = observed_data
    elif observed_data is not None:
        block_outputs["current_page_observation"] = str(observed_data)
    return RunEvidenceSnapshot(
        workflow_run_id=run_id if isinstance(run_id, str) else None,
        block_outputs=block_outputs,
        current_url=_valid_runtime_anchor_url(url),
        page_title=title if isinstance(title, str) and title.strip() else None,
    )


async def _maybe_run_completion_verification_from_page_observation(
    copilot_ctx: Any,
    *,
    url: str,
    title: str = "",
    observed_data: object | None = None,
) -> CompletionVerificationResult | None:
    """Verify completion only for post-run page observations after failed tests."""

    if not settings.COPILOT_OUTCOME_VERIFICATION_ENABLED:
        return None
    existing = getattr(copilot_ctx, "completion_verification_result", None)
    if isinstance(existing, CompletionVerificationResult) and existing.is_fully_satisfied():
        return existing
    if getattr(copilot_ctx, "post_run_page_observation_after_failed_test", False) is not True:
        return None
    criteria = _completion_verification_criteria(copilot_ctx)
    if not criteria:
        return None
    handler = await _completion_verification_handler(copilot_ctx)
    if handler is None:
        return None
    remaining = _copilot_seconds_remaining(copilot_ctx)
    if (
        remaining is not None
        and remaining
        <= settings.COPILOT_FEASIBILITY_GATE_TIMEOUT_SECONDS + _COMPLETION_VERIFICATION_BUDGET_MARGIN_SECONDS
    ):
        verification = CompletionVerificationResult(status="unavailable")
    else:
        snapshot = _build_page_observation_evidence_snapshot(
            copilot_ctx,
            url=url,
            title=title,
            observed_data=observed_data,
        )
        if not snapshot.has_evidence():
            verification = CompletionVerificationResult(
                status="evaluated",
                criterion_ids=[criterion.id for criterion in criteria],
                verdicts=[
                    CriterionVerdict(criterion_id=criterion.id, satisfied=False, reason_code="no_evidence")
                    for criterion in criteria
                ],
            )
        else:
            verification = await evaluate_completion_criteria(criteria, snapshot, handler)

    if (
        isinstance(existing, CompletionVerificationResult)
        and not verification.is_fully_satisfied()
        and not (existing.status == "unavailable" and verification.status == "evaluated")
    ):
        return existing

    copilot_ctx.completion_verification_result = verification
    record_completion_verification(copilot_ctx, verification)
    if verification.status == "evaluated":
        _emit_completion_verification_trace(copilot_ctx, verification)
    return verification


# Block types the copilot must never emit. They delegate the entire goal to
# a separate agent, which bypasses copilot-level block decomposition and
# obfuscates issues the copilot should surface/handle directly.
_COPILOT_BANNED_BLOCK_TYPES: frozenset[str] = frozenset({"task", "task_v2"})
# Code-only browser mode uses this temporary hard ban to force iteration on
# code-first Copilot behavior. The GA policy is not settled: it may allow some
# of these block types again, or make the allow/ban list configurable.
_COPILOT_CODE_ONLY_BROWSER_BANNED_BLOCK_TYPES: frozenset[str] = _COPILOT_BANNED_BLOCK_TYPES | frozenset(
    {
        "action",
        "browser_task",
        "extraction",
        "file_download",
        "file_upload",
        "goto_url",
        "login",
        "navigation",
        "print_page",
        "validation",
    }
)

# Shared suffix across every LLM-facing rejection message for banned
# block emission — the pre-hook (schema-lookup reject) and the post-
# emission detector both steer the LLM toward the same alternatives.
_COPILOT_BANNED_BLOCK_ALTERNATIVES = (
    "Use `navigation` for page actions (filling forms, clicking, multi-step flows), "
    "`extraction` for data extraction, `validation` for completion checks, "
    "`login` for authentication, or `goto_url` for pure URL navigation."
)
_COPILOT_CODE_ONLY_BROWSER_ALTERNATIVES = (
    "Browser/page workflow block types are unavailable in code-only browser mode. Use focused `code` "
    "blocks for durable page or browser-session work."
)
_CODE_ONLY_TARGET_EVIDENCE_KEYS = frozenset(
    {
        "buttons",
        "fields",
        "forms",
        "inputs",
        "links",
        "options",
        "result",
        "results",
        "rows",
        "selects",
        "tables",
        "textareas",
        "url",
    }
)
_CODE_ONLY_SELECTOR_ACTION_TOOLS = frozenset({"click", "type_text", "select_option", "press_key"})


def _copilot_block_authoring_policy(ctx: AgentContext | None) -> BlockAuthoringPolicy:
    if ctx is None:
        return BlockAuthoringPolicy.STANDARD
    return normalize_block_authoring_policy(getattr(ctx, "block_authoring_policy", None))


def _copilot_banned_block_types(ctx: AgentContext | None) -> frozenset[str]:
    if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return _COPILOT_CODE_ONLY_BROWSER_BANNED_BLOCK_TYPES
    return _COPILOT_BANNED_BLOCK_TYPES


def _copilot_banned_block_alternatives(ctx: AgentContext | None) -> str:
    if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return _COPILOT_CODE_ONLY_BROWSER_ALTERNATIVES
    return _COPILOT_BANNED_BLOCK_ALTERNATIVES


def _banned_block_reject_message(items: list[tuple[str, str]], ctx: AgentContext | None = None) -> str:
    """Uniform error text for the post-emission reject, sharing the
    alternatives suffix with the schema pre-hook."""
    labels = ", ".join(sorted({label for label, _ in items}))
    types = sorted({block_type for _, block_type in items})
    types_part = " / ".join(repr(t) for t in types)
    return (
        f"Block type {types_part} is not available in the workflow copilot. "
        f"Offending labels: [{labels}]. "
        f"{_copilot_banned_block_alternatives(ctx)}"
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


def _collect_banned_block_items(
    blocks: list[Any],
    banned_types: frozenset[str] | None = None,
) -> list[tuple[str, str]]:
    """Recursively walk ``blocks`` (mirroring
    :func:`skyvern.forge.sdk.copilot.block_goal_wrapping._wrap_blocks_in_place`)
    and return ``(label, normalized_block_type)`` for every block whose type is
    in :data:`_COPILOT_BANNED_BLOCK_TYPES`. Blocks missing ``label`` are
    skipped — the downstream Pydantic validator surfaces those errors on its
    own."""
    active_banned_types = banned_types or _COPILOT_BANNED_BLOCK_TYPES
    items: list[tuple[str, str]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        raw_type = block.get("block_type")
        if isinstance(raw_type, str):
            normalized = raw_type.strip().lower()
            raw_normalized = normalize_copilot_block_type_alias(normalized)
            if normalized in active_banned_types or raw_normalized in active_banned_types:
                label = block.get("label")
                if isinstance(label, str):
                    items.append((label, raw_normalized))
        loop_blocks = block.get("loop_blocks")
        if isinstance(loop_blocks, list):
            items.extend(_collect_banned_block_items(loop_blocks, active_banned_types))
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


def _block_label_from_yaml(block: dict[str, Any]) -> str | None:
    label = block.get("label")
    return label if isinstance(label, str) and label else None


def _detect_new_banned_blocks(
    submitted_yaml: str,
    prior_workflow_yaml: str | None,
    *,
    banned_types: frozenset[str] | None = None,
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
    active_banned_types = banned_types or _COPILOT_BANNED_BLOCK_TYPES
    submitted_items = _collect_banned_block_items(submitted_blocks, active_banned_types)
    if not submitted_items:
        return []
    prior_blocks = _parse_workflow_blocks(prior_workflow_yaml)
    prior_labels = {label for label, _ in _collect_banned_block_items(prior_blocks or [], active_banned_types)}
    return [(label, block_type) for label, block_type in submitted_items if label not in prior_labels]


_CHALLENGE_WAIT_PATTERN = re.compile(
    r"\b(anti[-_\s]?bot|bot[-_\s]?block|captcha|challenge|human[-_\s]?verification|ip[-_\s]?block|waf)\b",
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


def _composition_evidence_has_challenge(ctx: AgentContext) -> bool:
    evidence = getattr(ctx, "composition_page_evidence", None)
    if not isinstance(evidence, dict):
        return False
    if evidence.get("anti_bot_indicators") or evidence.get("challenge_controls"):
        return True
    challenge_state = evidence.get("challenge_state")
    return isinstance(challenge_state, dict) and challenge_state.get("detected") is True


def _detect_new_http_request_blocks(submitted_yaml: str | None, prior_workflow_yaml: str | None) -> list[str]:
    submitted_blocks = _parse_workflow_blocks(submitted_yaml)
    if submitted_blocks is None:
        return []
    prior_blocks = _parse_workflow_blocks(prior_workflow_yaml)
    prior_labels: set[str] = set()
    for block in _iter_yaml_blocks(prior_blocks or []):
        if str(block.get("block_type") or "").strip().lower() != "http_request":
            continue
        label = block.get("label")
        if isinstance(label, str):
            prior_labels.add(label)
    labels: list[str] = []
    for block in _iter_yaml_blocks(submitted_blocks):
        if str(block.get("block_type") or "").strip().lower() != "http_request":
            continue
        label = block.get("label")
        if isinstance(label, str) and label not in prior_labels:
            labels.append(label)
    return labels


def _challenge_http_request_reject_message(
    ctx: AgentContext, submitted_yaml: str | None, prior_workflow_yaml: str | None
) -> str | None:
    if not _composition_evidence_has_challenge(ctx):
        return None
    labels = _detect_new_http_request_blocks(submitted_yaml, prior_workflow_yaml)
    if not labels:
        return None
    labels_text = ", ".join(sorted(set(labels)))
    return (
        "Workflow validation failed: raw http_request blocks are not allowed for a page with observed "
        "anti-bot or human-verification challenge evidence. "
        f"Offending labels: [{labels_text}]. "
        "Use browser workflow blocks grounded in the observed page, include challenge handling only when visible, "
        "or stop and report the observed challenge blocker if it cannot be completed."
    )


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
    normalized = normalize_copilot_block_type_alias(block_type)
    if normalized != block_type.strip().lower():
        params["block_type"] = normalized
    if normalized not in _copilot_banned_block_types(ctx):
        return None
    return {
        "ok": False,
        "error": f"Block type {block_type!r} is not available in the workflow copilot. {_copilot_banned_block_alternatives(ctx)}",
    }


async def _validate_block_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return None
    return {
        "ok": False,
        "error": (
            "CODE-ONLY BLOCK VALIDATION DISABLED: do not use validate_block, dummy code blocks, or probe code "
            "blocks in code-only browser mode. Use MCP browser tools to explore the page, then call "
            "update_and_run_blocks with real focused code blocks that implement the workflow behavior."
        ),
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
            for banned in _copilot_banned_block_types(ctx):
                block_types.pop(banned, None)
        block_type = data.get("block_type")
        if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER and block_type == "code":
            ctx.code_only_code_schema_seen = True
            data["code_only_note"] = _COPILOT_CODE_ONLY_BROWSER_ALTERNATIVES
            data["code_only_guidance"] = [
                "Use one focused code block per durable browser goal, such as open, search, submit, expand, or extract.",
                "Do not persist navigation/action/login/extraction/validation blocks for browser page work.",
                "Use concrete selectors and text anchors found during exploration. If only intent targeting is available, inspect the page again before mutating.",
                "Call update_and_run_blocks with a connected runnable set of real code blocks instead of validating dummy or probe blocks.",
                "Keep block outputs JSON-safe and include visible evidence text when extracting records, products, totals, confirmations, or identifiers.",
            ]
    return result


def _code_only_pre_run_results_error(ctx: CopilotContext) -> dict[str, Any] | None:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return None
    if ctx.workflow_persisted or ctx.update_workflow_called:
        return None
    for value in (
        ctx.pending_reconciliation_run_id,
        ctx.last_run_blocks_workflow_run_id,
        ctx.last_successful_run_blocks_workflow_run_id,
    ):
        if isinstance(value, str) and value:
            return None
    return {
        "ok": False,
        "error": (
            "CODE-ONLY EXPLORATION PHASE: get_run_results is unavailable before a real workflow run exists. "
            "Use MCP browser tools such as navigate_browser, evaluate, click, type_text, get_browser_screenshot, "
            "console_messages, scroll, select_option, or press_key to understand the page, then call "
            "update_and_run_blocks with real focused code blocks."
        ),
    }


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


def _code_only_deterministic_targeting_error(tool_name: str) -> str:
    return (
        f"In code-only browser mode, {tool_name} requires a CSS/XPath selector for page mutations "
        "after the reusable workflow has been verified. Use evaluate, screenshots, or page inspection "
        "to derive a selector, then retry with selector only."
    )


def _code_only_selector_action_requires_deterministic_target(ctx: AgentContext) -> bool:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return False
    if not getattr(ctx, "workflow_persisted", False):
        return False
    return bool(getattr(ctx, "last_full_workflow_test_ok", False))


def _strip_intent_for_code_only_selector_action(
    params: dict[str, Any],
    ctx: AgentContext,
    *,
    tool_name: str,
) -> dict[str, Any] | None:
    if not _code_only_selector_action_requires_deterministic_target(ctx):
        return None
    if tool_name not in _CODE_ONLY_SELECTOR_ACTION_TOOLS:
        return None
    selector = params.get("selector")
    if isinstance(selector, str) and selector.strip():
        if "intent" in params:
            params["intent"] = None
        return None
    if params.get("intent"):
        return {"ok": False, "error": _code_only_deterministic_targeting_error(tool_name)}
    return None


def _code_only_has_target_page_evidence(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    for key, value in data.items():
        normalized = str(key).strip().lower()
        if normalized in _CODE_ONLY_TARGET_EVIDENCE_KEYS and bool(value):
            return True
        if isinstance(value, dict) and _code_only_has_target_page_evidence(value):
            return True
        if isinstance(value, list) and any(_code_only_has_target_page_evidence(item) for item in value):
            return True
    return False


_JQUERY_SELECTOR_RE = re.compile(r":(?:contains|eq|first|last|gt|lt|nth|visible|hidden|checked)\s*\(", re.IGNORECASE)


async def _click_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    await _capture_scout_source_url(ctx)
    deterministic_result = _strip_intent_for_code_only_selector_action(params, ctx, tool_name="click")
    if deterministic_result is not None:
        return deterministic_result
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


async def _type_text_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    await _capture_scout_source_url(ctx)
    return _strip_intent_for_code_only_selector_action(params, ctx, tool_name="type_text")


async def _select_option_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    await _capture_scout_source_url(ctx)
    return _strip_intent_for_code_only_selector_action(params, ctx, tool_name="select_option")


async def _press_key_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    await _capture_scout_source_url(ctx)
    return _strip_intent_for_code_only_selector_action(params, ctx, tool_name="press_key")


async def _navigate_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    _clear_pending_browser_interaction_observation(ctx)
    if result.get("ok"):
        data = result.pop("data", {})
        result["url"] = data.get("url", "")
        result["next_step"] = (
            "Page loaded. You MUST now use evaluate, "
            "get_browser_screenshot, or click to inspect page content "
            "before responding."
        )
        if (
            _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER
            and isinstance(ctx, CopilotContext)
            and ctx.build_phase in {BuildPhase.INITIAL, BuildPhase.DISCOVERING}
        ):
            advance_to_composing(ctx, reason="code_only_browser_navigation_succeeded")
    return result


async def _screenshot_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        _mark_page_inspected(ctx)
        data = result["data"]
        url, title = await _resolve_url_title(raw, ctx)
        _record_composition_page_observation(ctx, source_tool="get_browser_screenshot", url=url, title=title)
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
    _clear_pending_browser_interaction_observation(ctx)
    source_url = _consume_scout_source_url(ctx)
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url, title = await _resolve_url_title(raw, ctx)
        _mark_pending_browser_interaction_observation(ctx, tool_name="click", url=url)
        result["data"] = {
            "selector": data.get("selector", ""),
            "url": url,
            "title": title,
        }
        _record_scouted_interaction(ctx, tool_name="click", selector=data.get("selector", ""), source_url=source_url)
        observation_step = _register_scout_interaction_observation(
            ctx, tool_name="click", selector=data.get("selector", ""), source_url=source_url, url=url
        )
        if observation_step is not None:
            result["observation_step"] = observation_step
            result["data"]["observation_step"] = observation_step
    return result


_TYPE_READBACK_SETTLE_SECONDS = 0.3


async def _verify_scout_type_landed(
    ctx: AgentContext,
    *,
    selector: str,
    typed_length: Any,
) -> dict[str, Any] | None:
    """Confirm a non-empty type actually entered the field, else return a failure.

    A marketing/cookie overlay can consume the focus or keystrokes — the field
    stays empty while `skyvern_type` still reports success (the first interaction
    on an overlaid page often just dismisses the overlay). Read the field back; a
    field still empty after a non-empty type means the input did not land. Only
    fires when there is a selector to read and a positive typed length, so it never
    second-guesses intent-only types or masked/formatted values, which keep a
    non-empty value.
    """
    if not isinstance(selector, str) or not selector.strip():
        return None
    if not isinstance(typed_length, int) or typed_length <= 0:
        return None
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return None

    async def _read_back() -> Any:
        try:
            readback = await asyncio.wait_for(
                server.call_internal_tool("skyvern_get_value", {"selector": selector}),
                timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
            )
        except Exception:
            LOG.debug("scout type-landed read-back failed; leaving the type result unverified", exc_info=True)
            return None
        if not isinstance(readback, dict) or not readback.get("ok"):
            return None
        return (readback.get("data") or {}).get("value")

    value = await _read_back()
    if isinstance(value, str) and value.strip() == "":
        # A controlled/React input can mirror its value asynchronously, so a first read may be
        # transiently empty; settle briefly and re-read once before declaring the type lost.
        await asyncio.sleep(_TYPE_READBACK_SETTLE_SECONDS)
        value = await _read_back()
    if isinstance(value, str) and value.strip() == "":
        return {
            "ok": False,
            "error": (
                "type_text reported success but the field is still empty — an overlay "
                "(cookie/marketing popup) likely consumed the keystrokes or focus. "
                "Re-inspect the current page and retry typing into the target field; "
                "the overlay is usually dismissed by that first interaction."
            ),
        }
    return None


async def _type_text_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    _clear_pending_browser_interaction_observation(ctx)
    source_url = _consume_scout_source_url(ctx)
    if result.get("ok") and result.get("data"):
        data = result["data"]
        selector = data.get("selector", "")
        typed_length = data.get("text_length", 0)
        url, _ = await _resolve_url_title(raw, ctx)
        result["data"] = {
            "selector": selector,
            "typed_length": typed_length,
            "url": url,
        }
        landing_failure = await _verify_scout_type_landed(ctx, selector=selector, typed_length=typed_length)
        if landing_failure is not None:
            return landing_failure
        _mark_pending_browser_interaction_observation(ctx, tool_name="type_text", url=url)
        _record_scouted_interaction(
            ctx, tool_name="type_text", selector=selector, source_url=source_url, typed_length=typed_length
        )
        observation_step = _register_scout_interaction_observation(
            ctx, tool_name="type_text", selector=selector, source_url=source_url, url=url
        )
        if observation_step is not None:
            result["observation_step"] = observation_step
            result["data"]["observation_step"] = observation_step
    return result


async def _evaluate_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        _mark_page_inspected(ctx)
        result["data"].pop("sdk_equivalent", None)
        if "url" not in result["data"]:
            url, _ = await _resolve_url_title(raw, ctx)
            if url:
                result["data"]["url"] = url
        url = str(result["data"].get("url") or "")
        title = str(result["data"].get("title") or "")
        if not title:
            _, title = await _resolve_url_title(raw, ctx)
        observation_step = _record_composition_page_observation(
            ctx,
            source_tool="evaluate",
            url=url,
            title=title,
            observed_data=result["data"],
            append_to_flow=True,
            reached_via="auto",
        )
        if observation_step is not None:
            result["observation_step"] = observation_step
            result["data"]["observation_step"] = observation_step
        if _copilot_block_authoring_policy(
            ctx
        ) == BlockAuthoringPolicy.CODE_ONLY_BROWSER and _code_only_has_target_page_evidence(result["data"]):
            ctx.code_only_target_page_evidence_seen = True
        await _maybe_run_completion_verification_from_page_observation(
            ctx,
            url=url,
            title=title,
            observed_data=result["data"],
        )
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
    _clear_pending_browser_interaction_observation(ctx)
    source_url = _consume_scout_source_url(ctx)
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url, _ = await _resolve_url_title(raw, ctx)
        _mark_pending_browser_interaction_observation(ctx, tool_name="select_option", url=url)
        result["data"] = {
            "selector": data.get("selector", ""),
            "value": data.get("value", ""),
            "url": url,
        }
        _record_scouted_interaction(
            ctx,
            tool_name="select_option",
            selector=data.get("selector", ""),
            source_url=source_url,
            value=data.get("value", ""),
        )
        observation_step = _register_scout_interaction_observation(
            ctx, tool_name="select_option", selector=data.get("selector", ""), source_url=source_url, url=url
        )
        if observation_step is not None:
            result["observation_step"] = observation_step
            result["data"]["observation_step"] = observation_step
    return result


async def _press_key_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    _clear_pending_browser_interaction_observation(ctx)
    source_url = _consume_scout_source_url(ctx)
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url, _ = await _resolve_url_title(raw, ctx)
        _mark_pending_browser_interaction_observation(ctx, tool_name="press_key", url=url)
        result["data"] = {
            "key": data.get("key", ""),
            "selector": data.get("selector", ""),
            "url": url,
        }
        _record_scouted_interaction(
            ctx,
            tool_name="press_key",
            selector=data.get("selector", ""),
            source_url=source_url,
            key=data.get("key", ""),
        )
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
        "validate_block": SchemaOverlay(pre_hook=_validate_block_pre_hook),
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
                "Click an element in the browser. Prefer a CSS selector ALONE for a target "
                "you can identify from page evidence — a selector-only click is instant and "
                "deterministic. Use `intent` only when you cannot derive a selector: an "
                "`intent`-only click routes through a slower full-page AI scan, and if you "
                "pass both, the selector wins and the `intent` is ignored. When a shared class "
                "matches many elements (e.g. one button per result row), scope the selector to "
                "the specific item (its container, a unique attribute, or :nth-of-type) instead "
                "of relying on `intent` to disambiguate. "
                "IMPORTANT: jQuery pseudo-selectors like :contains(), :eq(), :first, "
                ":visible are NOT valid CSS. Use standard selectors: "
                "'button.download', 'a[href*=\"pdf\"]', '#submit-btn', "
                "'table tr:nth-of-type(2) td a'."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "button", "click_count"}),
            forced_args={"selector_mode": "direct"},
            requires_browser=True,
            timeout=15,
            pre_hook=_click_pre_hook,
            post_hook=_click_post_hook,
        ),
        "type_text": SchemaOverlay(
            description=(
                "Type text into an input element. Prefer a CSS selector ALONE to target the "
                "field — a selector-only type is instant and deterministic. Use `intent` only "
                "when you cannot derive a selector: an `intent`-only type routes through a "
                "slower full-page AI scan, and if you pass both, the selector wins and the "
                "`intent` is ignored. "
                "Optionally clear the field first. Use this for form filling. "
                "NEVER type inline passwords, API keys, tokens, cookies, TOTP/OTP "
                "codes, private keys, or other raw credentials/secrets received in "
                "chat — stop and follow the CREDENTIAL HANDLING refusal rule in the "
                "system prompt instead."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "delay"}),
            forced_args={"selector_mode": "direct"},
            required_overrides=["text"],
            arg_transforms={"clear_first": "clear"},
            requires_browser=True,
            timeout=15,
            pre_hook=_type_text_pre_hook,
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
                "Select an option from a <select> dropdown. Provide the value to select and a "
                "selector to target the element precisely; use `intent` (alone) only when you "
                "cannot derive a selector — passing both lets the selector win and ignores the "
                "`intent`. For free-text inputs, use type_text instead."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "timeout"}),
            forced_args={"selector_mode": "direct"},
            required_overrides=["value"],
            requires_browser=True,
            timeout=15,
            pre_hook=_select_option_pre_hook,
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
            pre_hook=_press_key_pre_hook,
            post_hook=_press_key_post_hook,
        ),
    }


def _record_workflow_update_result(
    copilot_ctx: Any, result: dict[str, Any], prior_definition: object | None = None
) -> None:
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

    # Block-running failures keyed off (labels, parameters) go stale once the
    # workflow itself changes — without this clear, a user who fixes the bug
    # via update_workflow gets a LOOP DETECTED on the next legitimate run.
    clear_failed_step_tracker_for_tools_in_ctx(copilot_ctx, BLOCK_RUNNING_TOOLS)

    _invalidate_verified_state_on_edit(copilot_ctx, prior_definition, getattr(wf, "workflow_definition", None))


def _last_update_is_single_goto_bootstrap(copilot_ctx: CopilotContext) -> bool:
    last_workflow = copilot_ctx.last_workflow
    definition = _workflow_definition_as_dict(last_workflow.workflow_definition if last_workflow is not None else None)
    blocks = definition.get("blocks")
    if not isinstance(blocks, list) or len(blocks) != 1:
        return False
    block = blocks[0]
    if not isinstance(block, dict):
        return False
    return str(block.get("block_type") or "").strip().lower() == "goto_url"


def _pre_run_workflow_coverage_error(copilot_ctx: Any) -> str | None:
    block_count = getattr(copilot_ctx, "last_update_block_count", None)
    if not isinstance(block_count, int):
        return None
    if block_count == 1 and _last_update_is_single_goto_bootstrap(copilot_ctx):
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
                structured_blocker = _structured_blocker_message(payload)
                if structured_blocker:
                    texts_to_scan.append(structured_blocker)
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


def _composition_anti_bot_reason(copilot_ctx: object) -> str | None:
    evidence = getattr(copilot_ctx, "composition_page_evidence", None)
    if not isinstance(evidence, dict):
        return None
    indicators = evidence.get("anti_bot_indicators")
    challenge_controls = evidence.get("challenge_controls")
    challenge_state = evidence.get("challenge_state")
    normalized_indicators = (
        [str(item) for item in indicators if isinstance(item, str)] if isinstance(indicators, list) else []
    )
    control_count = len(challenge_controls) if isinstance(challenge_controls, list) else 0
    challenge_detected = isinstance(challenge_state, dict) and challenge_state.get("detected") is True
    if not normalized_indicators and control_count == 0 and not challenge_detected:
        return None
    detail_parts = normalized_indicators[:4]
    if isinstance(challenge_state, dict):
        state_indicators = challenge_state.get("indicators")
        if isinstance(state_indicators, list):
            detail_parts.extend(str(item) for item in state_indicators if isinstance(item, str))
        challenge_kind = challenge_state.get("kind")
        if isinstance(challenge_kind, str) and challenge_kind and challenge_kind != "none":
            detail_parts.append(challenge_kind)
        gated_controls = challenge_state.get("gated_submit_controls")
        if challenge_state.get("gates_submit_controls") is True:
            gated_control_items = gated_controls if isinstance(gated_controls, list) else []
            control_labels = [
                str(item.get("text") or item.get("value") or item.get("id") or item.get("name") or item.get("selector"))
                for item in gated_control_items
                if isinstance(item, dict)
                and (
                    item.get("text") or item.get("value") or item.get("id") or item.get("name") or item.get("selector")
                )
            ]
            labels = ", ".join(list(dict.fromkeys(control_labels))[:3]) or "submit/search control"
            detail_parts.append(f"challenge-gated disabled submit/search control: {labels}")
    if control_count:
        detail_parts.append(f"{control_count} challenge control(s)")
    details = ", ".join(list(dict.fromkeys(detail_parts))[:6])
    return f"Observed anti-bot challenge evidence before the run: {details}"


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


def _update_verification_evidence_from_run_result(copilot_ctx: AgentContext, result: Mapping[str, object]) -> None:
    evidence = _workflow_verification_evidence(copilot_ctx)
    data_value = result.get("data")
    data: Mapping[str, object] = data_value if isinstance(data_value, dict) else {}
    run_ok = bool(result.get("ok", False))
    full_workflow_verified = run_ok and copilot_ctx.last_full_workflow_test_ok is True
    evidence.full_workflow_verified = full_workflow_verified
    evidence.test_attempted_but_incomplete = not full_workflow_verified

    run_id = data.get("workflow_run_id")
    if isinstance(run_id, str) and run_id.strip():
        evidence.workflow_run_id = run_id.strip()
    if _active_run_terminal_evidence_detected(result):
        evidence.active_run_terminal_evidence_detected = True
        evidence.live_page_state_verified = True
        evidence.verified_from_current_browser_state = True
        evidence.full_workflow_verified = False
        evidence.test_attempted_but_incomplete = True
        if isinstance(run_id, str) and run_id.strip():
            evidence.active_run_terminal_evidence_workflow_run_id = run_id.strip()
        sample_index = data.get("active_run_terminal_evidence_sample_index")
        if isinstance(sample_index, int):
            evidence.active_run_terminal_evidence_sample_index = sample_index
    current_url = _valid_runtime_anchor_url(data.get("current_url"))
    if current_url is not None:
        evidence.current_url = current_url
        evidence.live_page_state_verified = True
        if data.get("observed_after_workflow_run") is True:
            evidence.current_url_observed_after_workflow_run = True
            evidence.current_url_may_encode_runtime_state = bool(urlparse(current_url).query)
    page_title = data.get("page_title")
    if isinstance(page_title, str) and page_title.strip():
        evidence.page_title = " ".join(page_title.split())[:160]

    if run_ok:
        evidence.merge_verified_blocks(_completed_run_block_labels(data))
        unverified = list(copilot_ctx.last_unverified_block_labels or [])
        evidence.unverified_block_labels = list(dict.fromkeys(str(label) for label in unverified if str(label)))
        evidence.failed_block_labels = []
        # A completed-but-suspicious run (outcome unverified, null data, blocker)
        # keeps its failure reason so the evidence stays consistent with
        # test_attempted_but_incomplete instead of reading as a clean success.
        suspicious_reason = copilot_ctx.last_test_failure_reason if copilot_ctx.last_test_suspicious_success else None
        evidence.failure_reason = (
            " ".join(suspicious_reason.split())[:240]
            if isinstance(suspicious_reason, str) and suspicious_reason.strip()
            else None
        )
        if evidence.unverified_block_labels:
            evidence.verified_from_current_browser_state = True
        return

    failed_labels = _failed_run_block_labels(data)
    evidence.failed_block_labels = failed_labels
    if copilot_ctx.last_failure_category_top == PER_TOOL_BUDGET_FAILURE_CATEGORY:
        evidence.merge_per_tool_budget_blocks(failed_labels)
    failure_reason = copilot_ctx.last_test_failure_reason or result.get("error")
    if isinstance(failure_reason, str) and failure_reason.strip():
        evidence.failure_reason = " ".join(failure_reason.split())[:240]


_COMPLETION_VERIFICATION_BUDGET_MARGIN_SECONDS = 5.0


async def _completion_verification_handler(copilot_ctx: Any) -> Any | None:
    return await resolve_main_copilot_handler(
        getattr(copilot_ctx, "workflow_permanent_id", None),
        getattr(copilot_ctx, "organization_id", None),
    )


async def _active_run_terminal_monitor_enabled(copilot_ctx: Any) -> bool:
    if not settings.COPILOT_OUTCOME_VERIFICATION_ENABLED:
        return False
    if not getattr(copilot_ctx, "browser_session_id", None):
        return False
    if not getattr(copilot_ctx, "discovery_mcp_server", None):
        return False
    if not _completion_verification_criteria(copilot_ctx):
        return False
    return await _completion_verification_handler(copilot_ctx) is not None


def _active_run_terminal_evidence_needs_visual_fallback(evidence: dict[str, Any]) -> bool:
    if page_evidence_needs_visual_fallback(evidence):
        return True
    return evidence.get("screenshot_used") is not True


async def _active_run_terminal_evidence_sample(
    copilot_ctx: Any,
    *,
    workflow_run_id: str,
    labels_to_execute: list[str],
    sample_index: int,
) -> ActiveRunTerminalEvidenceSample | None:
    criteria = _completion_verification_criteria(copilot_ctx)
    if not criteria:
        return None
    handler = await _completion_verification_handler(copilot_ctx)
    if handler is None:
        return None

    current_url_raw, page_title_raw = await _fallback_page_info(copilot_ctx)
    current_url = _valid_runtime_anchor_url(current_url_raw)
    if current_url is None:
        return None

    evidence, html_error = await _capture_composition_evidence(
        copilot_ctx,
        inspected_url=current_url,
        current_url=current_url,
        active_run_terminal_sample=True,
    )
    if html_error is not None or evidence is None:
        LOG.info(
            "copilot active-run terminal evidence sample skipped",
            workflow_run_id=workflow_run_id,
            sample_index=sample_index,
            html_error=html_error,
        )
        return None

    page_title = evidence.get("page_title")
    if not isinstance(page_title, str) or not page_title.strip():
        page_title = page_title_raw if isinstance(page_title_raw, str) and page_title_raw.strip() else None
    evidence = {
        **evidence,
        "workflow_run_id": workflow_run_id,
        "observed_during_active_workflow_run": True,
    }
    snapshot = RunEvidenceSnapshot(
        workflow_run_id=workflow_run_id,
        current_url=current_url,
        page_title=page_title,
        executed_block_labels=list(labels_to_execute),
        page_evidence=evidence,
    )
    if not snapshot.has_evidence():
        return None

    result = await evaluate_completion_criteria(criteria, snapshot, handler)
    LOG.info(
        "copilot active-run terminal evidence sample",
        workflow_run_id=workflow_run_id,
        sample_index=sample_index,
        completion_verification_status=result.status,
        completion_verification_fully_satisfied=result.is_fully_satisfied(),
    )
    if result.status != "evaluated" or not result.is_fully_satisfied():
        return None
    copilot_ctx.composition_page_evidence = evidence
    return ActiveRunTerminalEvidenceSample(
        current_url=current_url,
        page_title=page_title,
        page_evidence=evidence,
        completion_verification=result,
        sample_index=sample_index,
    )


def _active_run_terminal_evidence_result(
    *,
    workflow_run_id: str,
    run_status: str | None,
    sample: Any,
    requested_block_labels: list[str],
    executed_block_labels: list[str],
    current_url: str | None = None,
    page_title: str | None = None,
) -> dict[str, Any]:
    observed_url = current_url or getattr(sample, "current_url", None)
    observed_title = page_title or getattr(sample, "page_title", None)
    completion = getattr(sample, "completion_verification", None)
    completion_trace = completion.to_trace_data() if isinstance(completion, CompletionVerificationResult) else {}
    reason = (
        "The active run reached the requested browser state while the workflow was still running, "
        "so Copilot interrupted it before further browser actions could overshoot that state. "
        "The current page evidence is not a durable full-workflow verification; inspect the run boundary, "
        "repair the workflow if needed, and verify the corrected workflow run."
    )
    return {
        "ok": False,
        "error": reason,
        "data": {
            "workflow_run_id": workflow_run_id,
            "overall_status": run_status,
            "failure_reason": reason,
            "requested_block_labels": list(requested_block_labels),
            "executed_block_labels": list(executed_block_labels),
            "current_url": observed_url,
            "page_title": observed_title,
            "active_run_terminal_evidence_detected": True,
            "active_run_terminal_evidence_sample_index": getattr(sample, "sample_index", None),
            "full_workflow_verified": False,
            "current_page_evidence": getattr(sample, "page_evidence", None),
            "active_run_terminal_completion_verification": completion_trace,
            "failure_categories": [
                {
                    "category": ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY,
                    "confidence_float": 1.0,
                    "reasoning": (
                        "Bounded current-page evidence matched the request-policy completion criteria "
                        "while the workflow run was still active; the run was interrupted for diagnosis/repair."
                    ),
                }
            ],
        },
    }


def _is_outcome_evidence_candidate(copilot_ctx: Any, result: dict[str, Any]) -> bool:
    """A clean ok=True run worth judging on its whole-workflow outcome.

    Recognition is governed by the outcome evidence the user can observe, not by
    whether every block was verified as an end-to-end prefix (SKY-10576). The judge
    requires positive evidence for every criterion, and ``empty_data_blocks`` rejects
    a run whose outcome block produced nothing, so an incomplete run never passes --
    this only lets an already-reached goal be recognized without a redundant
    full-prefix re-run.
    """
    if not bool(result.get("ok", False)):
        return False
    if _run_blocks_structured_blocker_message(result):
        return False
    _anti_bot, empty_data_blocks, _categories = _analyze_run_blocks(result)
    return not empty_data_blocks


def _is_unfinished_run_verification_candidate(copilot_ctx: Any, result: dict[str, Any]) -> bool:
    """A canceled/partial run (ok=False) still worth judging because it left runtime
    evidence behind. The judge confirms a criterion only on positive evidence, so a
    broken run never spuriously passes; this only lets a reached goal be recognized
    even though the run did not finish cleanly — recognition must not key on run status.
    """
    if bool(result.get("ok", False)):
        return False
    if _active_run_terminal_evidence_detected(result):
        return False
    if _run_blocks_structured_blocker_message(result):
        return False
    data = result.get("data")
    if not isinstance(data, dict):
        return False
    return _valid_runtime_anchor_url(data.get("current_url")) is not None


def _build_run_evidence_snapshot(copilot_ctx: Any, result: dict[str, Any]) -> RunEvidenceSnapshot:
    data = result.get("data")
    data = data if isinstance(data, dict) else {}
    current_label_order = _current_workflow_block_labels(copilot_ctx)
    current_labels = set(current_label_order)
    # Evidence must be what THIS run produced. ``verified_block_outputs`` accumulates
    # across incremental runs, so sourcing from it would let an output from a prior
    # run satisfy a criterion the current run never re-produced.
    blocks = data.get("blocks")
    block_outputs: dict[str, Any] = {}
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            label = block.get("label")
            output = block.get("extracted_data")
            if isinstance(label, str) and label in current_labels and _is_meaningful_extracted_data(output):
                block_outputs[label] = output
    executed = data.get("executed_block_labels")
    executed_block_labels = [str(label) for label in executed] if isinstance(executed, list) else []
    page_title = data.get("page_title")
    run_id = data.get("workflow_run_id")
    return RunEvidenceSnapshot(
        workflow_run_id=run_id if isinstance(run_id, str) else None,
        block_outputs=block_outputs,
        current_url=_valid_runtime_anchor_url(data.get("current_url")),
        page_title=page_title if isinstance(page_title, str) and page_title.strip() else None,
        executed_block_labels=executed_block_labels,
        verified_context_block_labels=_verified_context_block_labels_for_snapshot(
            copilot_ctx,
            current_label_order,
            executed_block_labels,
        ),
    )


async def _maybe_run_completion_verification(
    copilot_ctx: Any, result: dict[str, Any], handler_start: float
) -> CompletionVerificationResult | None:
    if not settings.COPILOT_OUTCOME_VERIFICATION_ENABLED:
        return None
    if getattr(copilot_ctx, "copilot_total_timeout_exceeded", False):
        return None
    criteria = _completion_verification_criteria(copilot_ctx)
    if not criteria:
        return None
    if not (
        _is_outcome_evidence_candidate(copilot_ctx, result)
        or _is_unfinished_run_verification_candidate(copilot_ctx, result)
    ):
        return None
    # A missing judge handler is an infra/config state, not a transient failure:
    # fall back to the prior gate rather than fail closed on every run.
    handler = await _completion_verification_handler(copilot_ctx)
    if handler is None:
        return None
    # Too little budget to verify a candidate run: fail closed (unavailable) rather
    # than let the run-status proxy claim an unverified outcome as success.
    remaining = RUN_BLOCKS_SAFETY_CEILING_SECONDS - (time.monotonic() - handler_start)
    if remaining <= settings.COPILOT_FEASIBILITY_GATE_TIMEOUT_SECONDS + _COMPLETION_VERIFICATION_BUDGET_MARGIN_SECONDS:
        return CompletionVerificationResult(status="unavailable")
    snapshot = _build_run_evidence_snapshot(copilot_ctx, result)
    if not snapshot.has_evidence():
        return CompletionVerificationResult(
            status="evaluated",
            criterion_ids=[criterion.id for criterion in criteria],
            verdicts=[
                CriterionVerdict(criterion_id=criterion.id, satisfied=False, reason_code="no_evidence")
                for criterion in criteria
            ],
        )
    return await evaluate_completion_criteria(criteria, snapshot, handler)


def _outcome_unverified_reason(
    copilot_ctx: Any, completion_verification: CompletionVerificationResult | None
) -> str | None:
    if completion_verification is None:
        return None
    if completion_verification.status == "evaluated":
        if completion_verification.is_fully_satisfied():
            return None
        policy = getattr(copilot_ctx, "request_policy", None)
        criteria = list(policy.completion_criteria) if policy is not None else []
        unmet = summarize_unsatisfied_outcomes(completion_verification, criteria)
        detail = f": {unmet}" if unmet else ""
        return (
            f"The run completed but did not demonstrate the goal outcome(s){detail}. "
            "Add an end-state confirmation (an extraction or validation block) that observes the outcome, then re-run."
        )
    # An 'unavailable' result reaches here only when verification was required;
    # fail closed and ask for a re-run rather than claim an unverified success.
    return (
        "The run completed but the goal outcome could not be verified (verification was unavailable). "
        "Re-run to verify the outcome before reporting success."
    )


def _outcome_failure_warrants_repair(
    copilot_ctx: Any, completion_verification: CompletionVerificationResult | None
) -> bool:
    """Whether an unmet outcome should route to suspicious-success/repair rather
    than continue-building.

    Contradicting evidence is always a real failure. Absence of evidence is a
    failure only once the workflow has an outcome-producing block; while the agent
    is still adding blocks toward the goal, an unmet criterion is expected, and the
    run should keep building. Terminal success stays withheld in both cases via the
    verification result, so this only governs the repair directive, not the gate.
    """
    if completion_verification is None:
        return False
    if any(verdict.reason_code == "evidence_contradicts" for verdict in completion_verification.verdicts):
        return True
    return _current_workflow_has_evidence_block(copilot_ctx)


def _tool_visible_result_after_completion_verification(
    copilot_ctx: Any,
    result: dict[str, Any],
    completion_verification: CompletionVerificationResult | None,
) -> dict[str, Any]:
    outcome_unverified_reason = _outcome_unverified_reason(copilot_ctx, completion_verification)
    if outcome_unverified_reason is None:
        return result
    if not _outcome_failure_warrants_repair(copilot_ctx, completion_verification):
        return result

    data = result.get("data")
    copied_data = dict(data) if isinstance(data, dict) else {}
    copied_data["failure_reason"] = outcome_unverified_reason
    copied_data["completion_verification"] = (
        completion_verification.to_trace_data() if completion_verification is not None else None
    )
    categories = copied_data.get("failure_categories")
    copied_categories = list(categories) if isinstance(categories, list) else []
    copied_categories.insert(
        0,
        {
            "category": "OUTCOME_UNVERIFIED",
            "confidence_float": 1.0,
            "reasoning": outcome_unverified_reason,
        },
    )
    copied_data["failure_categories"] = copied_categories
    return {
        **result,
        "ok": False,
        "error": outcome_unverified_reason,
        "data": copied_data,
    }


def _emit_completion_verification_trace(
    copilot_ctx: Any, completion_verification: CompletionVerificationResult
) -> None:
    block_count = getattr(copilot_ctx, "last_update_block_count", None)
    policy = getattr(copilot_ctx, "request_policy", None)
    contract = policy.completion_contract if policy is not None else None
    heuristic_would_block = isinstance(block_count, int) and _goal_likely_needs_more_blocks(
        getattr(copilot_ctx, "user_message", ""), block_count, contract
    )
    trace_data = {
        **completion_verification.to_trace_data(),
        "heuristic_would_block": heuristic_would_block,
        "evidence_block_present": _current_workflow_has_evidence_block(copilot_ctx),
        "warrants_repair": _outcome_failure_warrants_repair(copilot_ctx, completion_verification),
    }
    LOG.info(
        "copilot completion verification",
        **{f"completion_verification_{key}": value for key, value in trace_data.items()},
    )
    with copilot_span("completion_verification", data=trace_data):
        pass


def _record_run_blocks_result(
    copilot_ctx: Any, result: dict[str, Any], completion_verification: CompletionVerificationResult | None = None
) -> None:
    run_ok = bool(result.get("ok", False))
    data = result.get("data")
    run_id = data.get("workflow_run_id") if isinstance(data, dict) else None
    copilot_ctx.completion_verification_result = completion_verification
    record_completion_verification(copilot_ctx, completion_verification)
    if completion_verification is not None and completion_verification.status == "evaluated":
        _emit_completion_verification_trace(copilot_ctx, completion_verification)
    copilot_ctx.last_run_blocks_workflow_run_id = run_id if isinstance(run_id, str) else None
    copilot_ctx.last_successful_run_blocks_workflow_run_id = run_id if run_ok and isinstance(run_id, str) else None
    # Watchdog cancels normally count as ok=False; only a coincident total
    # timeout softens to ``None`` to keep the unvalidated WIP rescue open.
    cancelled_by_watchdog = result.get(_INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY) is True
    timeout_latched = bool(copilot_ctx.copilot_total_timeout_exceeded)
    copilot_ctx.last_test_ok = None if (cancelled_by_watchdog and timeout_latched) else run_ok
    copilot_ctx.last_full_workflow_test_ok = False
    copilot_ctx.last_unverified_block_labels = _unverified_current_workflow_labels(copilot_ctx)
    copilot_ctx.last_test_failure_reason = None
    copilot_ctx.last_test_suspicious_success = False
    copilot_ctx.suspicious_success_nudge_count = 0
    copilot_ctx.last_test_anti_bot = None
    prior_budget_flag = copilot_ctx.last_failure_category_top == PER_TOOL_BUDGET_FAILURE_CATEGORY
    copilot_ctx.last_failure_category_top = None
    copilot_ctx.last_test_non_retriable_nav_error = None
    copilot_ctx.post_run_page_observation_tool = None
    copilot_ctx.post_run_page_observation_url = None
    copilot_ctx.post_run_page_observation_workflow_run_id = None
    copilot_ctx.post_run_page_observation_after_failed_test = False
    copilot_ctx.post_run_current_page_inspection_workflow_run_id = None

    structured_blocker = _run_blocks_structured_blocker_message(result)
    anti_bot_match, empty_data_blocks, failure_categories = _analyze_run_blocks(result)
    if not anti_bot_match:
        anti_bot_match = _composition_anti_bot_reason(copilot_ctx)
    if anti_bot_match:
        copilot_ctx.last_test_anti_bot = anti_bot_match
    if failure_categories:
        top = failure_categories[0]
        if isinstance(top, dict):
            top_category = top.get("category")
            if isinstance(top_category, str):
                copilot_ctx.last_failure_category_top = top_category

    if copilot_ctx.last_failure_category_top == PER_TOOL_BUDGET_FAILURE_CATEGORY and isinstance(data, dict):
        current_url = _valid_runtime_anchor_url(data.get("current_url"))
        if current_url is not None:
            copilot_ctx.post_budget_page_inspection_required = True
            copilot_ctx.post_budget_page_inspection_url = current_url
            copilot_ctx.post_budget_page_inspection_run_id = run_id if isinstance(run_id, str) else None

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

    if _active_run_terminal_evidence_detected(result):
        _update_verification_evidence_from_run_result(copilot_ctx, result)
        signal = _active_run_terminal_evidence_signal(copilot_ctx, "update_and_run_blocks")
        if signal is not None:
            stash_blocker_signal(copilot_ctx, signal)

    if run_ok:
        _mark_page_inspected(copilot_ctx)
        if structured_blocker:
            failure_reason = f"Run completed, but extracted data reported a blocker: {structured_blocker}"
            copilot_ctx.last_test_ok = False
            copilot_ctx.last_test_suspicious_success = True
            copilot_ctx.last_test_failure_reason = failure_reason
            copilot_ctx.last_failed_workflow_yaml = getattr(copilot_ctx, "workflow_yaml", None)
            if not copilot_ctx.last_test_anti_bot and _looks_like_anti_bot_blocker(structured_blocker):
                copilot_ctx.last_test_anti_bot = f"Extracted data reported anti-bot blocker: {structured_blocker[:160]}"
            data = result.get("data")
            if isinstance(data, dict):
                data.setdefault("failure_reason", failure_reason)
                if copilot_ctx.last_test_anti_bot and not failure_categories:
                    anti_bot_category = {
                        "category": "ANTI_BOT_DETECTION",
                        "confidence_float": 0.7,
                        "reasoning": "Structured extracted data reported an anti-bot blocker.",
                    }
                    data["failure_categories"] = [anti_bot_category]
            update_repeated_failure_state(copilot_ctx, result)
            _update_verification_evidence_from_run_result(copilot_ctx, result)
            return
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
            _update_verification_evidence_from_run_result(copilot_ctx, result)
            return
        copilot_ctx.failed_test_nudge_count = 0
        copilot_ctx.null_data_streak_count = 0
        copilot_ctx.probable_site_block_streak_count = 0
        copilot_ctx.last_failed_workflow_yaml = None
        # Real success: clear the signature latch so a subsequent bad URL in
        # the same session can re-fire the stop nudge.
        copilot_ctx.non_retriable_nav_error_last_emitted_signature = None
        unverified = _unverified_current_workflow_labels(copilot_ctx)
        copilot_ctx.last_unverified_block_labels = unverified
        outcome_unverified_reason = _outcome_unverified_reason(copilot_ctx, completion_verification)
        if outcome_unverified_reason is not None and _outcome_failure_warrants_repair(
            copilot_ctx, completion_verification
        ):
            # The workflow already has a confirmation block, yet the produced
            # evidence does not demonstrate the outcome (or contradicts it). Treat
            # it as a suspicious success so the existing repair/partial machinery
            # fires. A mid-build run with no confirmation block yet falls through to
            # keep-building below; terminal success stays withheld either way via
            # the verification result.
            copilot_ctx.last_test_suspicious_success = True
            copilot_ctx.last_test_failure_reason = outcome_unverified_reason
            if isinstance(data, dict):
                data.setdefault("failure_reason", outcome_unverified_reason)
        elif not unverified:
            copilot_ctx.last_full_workflow_test_ok = True
            copilot_ctx.last_good_workflow = copilot_ctx.last_workflow
            copilot_ctx.last_good_workflow_yaml = copilot_ctx.last_workflow_yaml
        else:
            copilot_ctx.last_test_failure_reason = (
                "The last run verified only the current browser frontier; unverified workflow blocks remain: "
                + ", ".join(unverified[:8])
            )
        update_repeated_failure_state(copilot_ctx, result)
        _update_verification_evidence_from_run_result(copilot_ctx, result)
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
    if copilot_ctx.last_test_failure_reason is None:
        copilot_ctx.last_test_failure_reason = next(iter_failure_reasons(result), None)
    if result.get("error") and copilot_ctx.last_test_failure_reason is None:
        copilot_ctx.last_test_failure_reason = str(result["error"])
    update_repeated_failure_state(copilot_ctx, result)
    _update_verification_evidence_from_run_result(copilot_ctx, result)


def _record_diagnosis_repair_contract(
    copilot_ctx: Any,
    *,
    source_tool: str,
    result: dict[str, Any],
    workflow_updated: bool = False,
) -> DiagnosisRepairContract:
    contract = build_diagnosis_repair_contract(
        source_tool=source_tool,
        result=result,
        ctx=copilot_ctx,
        workflow_updated=workflow_updated,
    )
    copilot_ctx.latest_diagnosis_repair_contract = contract
    trace_data = contract.to_trace_data()
    LOG.info(
        "copilot diagnosis repair contract shadow",
        **{f"diagnosis_repair_{key}": value for key, value in trace_data.items()},
    )
    with copilot_span("diagnosis_repair_contract", data=trace_data):
        pass
    return contract


def _diagnosis_repair_tool_error(copilot_ctx: Any, source_tool: str, error: str) -> str:
    result = {"ok": False, "error": error}
    _record_diagnosis_repair_contract(copilot_ctx, source_tool=source_tool, result=result)
    return json.dumps(result)


@function_tool(
    name_override="update_workflow",
    tool_input_guardrails=[_WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL],
)
async def update_workflow_tool(
    ctx: RunContextWrapper,
    workflow_yaml: str,
    block_observation_refs: list[BlockObservationRef] | None = None,
    code_artifact_metadata: list[CodeArtifactMetadata] | None = None,
) -> str:
    """Validate and update the workflow YAML definition.
    Provide the complete workflow YAML as a string.
    Returns the validated workflow or validation errors.

    Top-level workflow parameter keys appear in the run-input UI. When you
    add runtime inputs in `workflow_definition.parameters`, name keys for the
    reusable domain value the user supplies, not the page widget or action used
    to enter it.

    Use browser inspection and run evidence to fill knowledge gaps while
    building or editing the workflow. Do not invent URL params, form fields,
    result affordances, or page structure from memory; ground workflow blocks
    in observed MCP evidence or information the user supplied.
    When you compose no-url blocks from a page reached by prior clicks, include
    `block_observation_refs` entries with each block label and the
    `observation_step` returned by inspect_page_for_composition for the page
    that block acts on.
    For authored code blocks, include `code_artifact_metadata` rows describing
    declared goals, claimed outcomes, page dependencies, criteria, evidence
    refs, observation refs, and terminal verifier expectations.
    """
    copilot_ctx = ctx.context
    serialized_code_artifact_metadata = _code_artifact_metadata_as_tool_argument(code_artifact_metadata)
    normalized_block_observation_refs = normalize_block_observation_refs(block_observation_refs)
    arguments = {
        "workflow_yaml": workflow_yaml,
        "block_observation_refs": normalized_block_observation_refs,
        "code_artifact_metadata": serialized_code_artifact_metadata,
    }
    loop_error = _tool_loop_error(copilot_ctx, "update_workflow", arguments)
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})
    credential_deferred_draft = _request_policy_allows_credential_deferred_draft(copilot_ctx)
    # A credential-deferred draft is redirected to update_and_run_blocks' skip-run
    # save path, which saves the draft and skips the browser run with the required
    # credential setup message.
    if credential_deferred_draft:
        agent_steering = (
            "Use update_and_run_blocks for this credential-deferred draft. It will save the workflow draft "
            "and skip the browser run with the required credential setup message."
        )
        user_facing = (
            "I can save this as a draft without running it because the credentials aren't set up yet. "
            "Add them in the Credentials UI and ask me to test the workflow."
        )
        signal = CopilotToolBlockerSignal(
            blocker_kind="authority_denied",
            agent_steering_text=agent_steering,
            user_facing_reason=user_facing,
            recovery_hint="retry_with_different_tool",
            cleared_by_tools=frozenset({"update_and_run_blocks"}),
            internal_reason_code="request_policy_credential_deferred_redirect",
            blocked_tool="update_workflow",
        )
        payload = _emit_tool_blocker_signal(copilot_ctx, signal)
        result = {"ok": False, "error": payload}
        record_tool_step_result_for_ctx(copilot_ctx, "update_workflow", arguments, result)
        return json.dumps(result)

    with copilot_span(
        "composition_evidence_precheck",
        data={**_COMPOSITION_EVIDENCE_PRECHECK_TRACE_DATA, "tool_name": "update_workflow"},
    ):
        composition_evidence_error = _update_and_run_blocks_composition_evidence_precheck(
            copilot_ctx,
            workflow_yaml,
            normalized_block_observation_refs,
            block_observation_refs,
        )
    if composition_evidence_error:
        result = {"ok": False, "error": composition_evidence_error}
        record_tool_step_result_for_ctx(copilot_ctx, "update_workflow", arguments, result)
        _record_diagnosis_repair_contract(
            copilot_ctx,
            source_tool="update_workflow",
            result=result,
        )
        sanitized = sanitize_tool_result_for_llm("update_workflow", result)
        return json.dumps(sanitized)

    prior_definition = await _get_prior_workflow_definition(copilot_ctx)
    with copilot_span("update_workflow", data={"yaml_length": len(workflow_yaml)}):
        result = await _update_workflow(
            {
                **arguments,
                "raw_block_observation_refs": block_observation_refs,
                "raw_code_artifact_metadata": code_artifact_metadata,
            },
            copilot_ctx,
            allow_missing_credentials=getattr(copilot_ctx, "allow_untested_workflow_draft", False) is True,
        )
        _record_workflow_update_result(copilot_ctx, result, prior_definition)
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

    authority_error = _authority_tool_error(copilot_ctx, "list_credentials")
    if authority_error:
        result = {"ok": False, "error": authority_error}
        record_tool_step_result_for_ctx(copilot_ctx, "list_credentials", arguments, result)
        return json.dumps(result)

    result = await _list_credentials(arguments, copilot_ctx)
    record_tool_step_result_for_ctx(copilot_ctx, "list_credentials", arguments, result)
    sanitized = sanitize_tool_result_for_llm("list_credentials", result)
    return json.dumps(sanitized)


def _run_blocks_span_data(
    block_labels: list[str],
    labels_to_execute: list[str],
    frontier_start_label: str | None,
    seeded_outputs: dict[str, Any],
    ctx: object,
) -> dict[str, Any]:
    return {
        "requested_block_labels": block_labels,
        "executed_block_labels": labels_to_execute,
        "frontier_start_label": frontier_start_label,
        "seeded_output_count": len(seeded_outputs or {}),
        "repeated_failure_streak_count": int(getattr(ctx, "repeated_failure_streak_count", 0) or 0),
        "block_count": len(block_labels),
    }


def _frontier_run_size_result(
    error: str,
    block_labels: list[str],
    labels_to_execute: list[str],
) -> dict[str, Any]:
    suggested_labels = list(labels_to_execute[:_MAX_INCREMENTAL_PAGE_FRONTIER_LABELS])
    user_facing_summary = (
        "Workflow draft saved; I still need to test the next smaller browser frontier before continuing."
    )
    return {
        "ok": False,
        "error": error,
        "data": {
            "workflow_run_id": None,
            "overall_status": "skipped",
            "workflow_run_skipped": True,
            "requested_block_labels": list(block_labels),
            "executed_block_labels": [],
            "planned_block_labels": list(labels_to_execute),
            "suggested_block_labels": suggested_labels,
            "deferred_block_labels": list(labels_to_execute[_MAX_INCREMENTAL_PAGE_FRONTIER_LABELS:]),
            "control_signal": {
                "kind": "intermediate_success",
                "user_facing_summary": user_facing_summary,
                "next_tool": "run_blocks_and_collect_debug",
                "next_block_labels": suggested_labels,
                "preserve_workflow_yaml": True,
            },
            "user_facing_summary": user_facing_summary,
        },
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

    For diagnostic complaints, follow the system prompt's ASK-vs-EDIT routing.
    If the complaint has no prior edit goal, inspect current workflow context
    and existing run evidence before deciding whether a fresh run is needed.
    If prior context establishes a resolvable edit, use `update_and_run_blocks`
    instead of rerunning unchanged blocks.

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

    Use browser inspection and run evidence to fill knowledge gaps before
    changing the workflow. If visible state is uncertain, inspect the live
    page and then compose the next normal workflow action from observed
    evidence instead of retrying guessed URL params or page structure.
    """
    copilot_ctx = ctx.context
    copilot_ctx.completion_verification_result = None
    handler_start = time.monotonic()
    arguments = {"block_labels": block_labels, "parameters": parameters or {}}
    authority_error = _authority_tool_error(copilot_ctx, "run_blocks_and_collect_debug")
    if authority_error:
        return _diagnosis_repair_tool_error(copilot_ctx, "run_blocks_and_collect_debug", authority_error)

    loop_error = _tool_loop_error(copilot_ctx, "run_blocks_and_collect_debug", arguments)
    if loop_error:
        return _diagnosis_repair_tool_error(copilot_ctx, "run_blocks_and_collect_debug", loop_error)

    prior_definition = await _get_prior_workflow_definition(copilot_ctx)
    labels_to_execute, block_outputs_to_seed, frontier_start_label = _plan_frontier(
        copilot_ctx, block_labels, prior_definition, prior_definition
    )
    frontier_error = _frontier_run_size_error(copilot_ctx, block_labels, labels_to_execute, prior_definition)
    if frontier_error:
        result = _frontier_run_size_result(frontier_error, block_labels, labels_to_execute)
        record_tool_step_result_for_ctx(copilot_ctx, "run_blocks_and_collect_debug", arguments, result)
        _record_diagnosis_repair_contract(
            copilot_ctx,
            source_tool="run_blocks_and_collect_debug",
            result=result,
        )
        return json.dumps(result)

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
        completion_verification = await _maybe_run_completion_verification(copilot_ctx, result, handler_start)
        _record_run_blocks_result(copilot_ctx, result, completion_verification=completion_verification)
        tool_visible_result = _tool_visible_result_after_completion_verification(
            copilot_ctx,
            result,
            completion_verification,
        )
        record_tool_step_result_for_ctx(copilot_ctx, "run_blocks_and_collect_debug", arguments, tool_visible_result)
        _record_diagnosis_repair_contract(
            copilot_ctx,
            source_tool="run_blocks_and_collect_debug",
            result=tool_visible_result,
        )
        enqueue_screenshot_from_result(copilot_ctx, result)

    sanitized = sanitize_tool_result_for_llm("run_blocks_and_collect_debug", tool_visible_result)
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
    authority_error = _authority_tool_error(copilot_ctx, "get_run_results")
    if authority_error:
        return json.dumps({"ok": False, "error": authority_error})
    if isinstance(copilot_ctx, CopilotContext):
        code_only_pre_run_error = _code_only_pre_run_results_error(copilot_ctx)
        if code_only_pre_run_error is not None:
            record_tool_step_result_for_ctx(copilot_ctx, "get_run_results", params, code_only_pre_run_error)
            return json.dumps(code_only_pre_run_error)

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
    block_observation_refs: list[BlockObservationRef] | None = None,
    code_artifact_metadata: list[CodeArtifactMetadata] | None = None,
    parameters: dict[str, Any] | None = None,
) -> Any:
    """Update the workflow YAML and immediately run the specified blocks in one step.
    Use this instead of calling update_workflow and run_blocks_and_collect_debug separately.
    The workflow must validate successfully before blocks are run.
    `block_labels` may be a tested frontier subset of the full workflow YAML;
    save the complete reusable workflow, then run only the next 1-2 unverified
    blocks when a long form/search/result chain can be verified incrementally.

    Top-level workflow parameter keys appear in the run-input UI. When you
    add runtime inputs in `workflow_definition.parameters`, name keys for the
    reusable domain value the user supplies, not the page widget or action used
    to enter it.

    For diagnostic complaints, follow the system prompt's ASK-vs-EDIT routing.
    A complaint with no prior edit goal needs context inspection or
    clarification first. A diagnostic follow-up after an explicit edit goal may
    update/run once the correction is clear.

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

    Use browser inspection and run evidence to fill knowledge gaps while
    building, editing, or debugging the workflow. Do not invent URL params,
    form fields, result affordances, or page structure from memory; ground
    workflow blocks in observed MCP evidence or information the user supplied.
    Only refine URL params when they are grounded in observed DOM/link/form
    state or observed URL deltas.
    Browser inspection is build-time context; add durable workflow blocks only
    for the reusable actions/checks the workflow actually needs.
    When you compose no-url blocks from a page reached by prior clicks, include
    `block_observation_refs` entries with each block label and the
    `observation_step` returned by inspect_page_for_composition or evaluate for
    the page that block acts on.
    For authored code blocks, include `code_artifact_metadata` rows describing
    declared goals, claimed outcomes, page dependencies, criteria, evidence
    refs, observation refs, and terminal verifier expectations.
    When inspected evidence shows an anti-bot challenge gating a disabled
    submit/search control, account for challenge resolution before submit;
    do not compose a click against a control observed as disabled.
    """
    copilot_ctx = ctx.context
    copilot_ctx.completion_verification_result = None
    handler_start = time.monotonic()
    serialized_code_artifact_metadata = _code_artifact_metadata_as_tool_argument(code_artifact_metadata)
    normalized_block_observation_refs = normalize_block_observation_refs(block_observation_refs)
    arguments = {
        "workflow_yaml": workflow_yaml,
        "block_labels": block_labels,
        "block_observation_refs": normalized_block_observation_refs,
        "code_artifact_metadata": serialized_code_artifact_metadata,
        "parameters": parameters or {},
    }
    skip_run_after_update = _request_policy_allows_update_and_skip_run(copilot_ctx, "update_and_run_blocks")
    authority_error = _authority_tool_error(
        copilot_ctx,
        "update_and_run_blocks",
        ignore_request_policy_error=skip_run_after_update,
    )
    if authority_error:
        return _diagnosis_repair_tool_error(copilot_ctx, "update_and_run_blocks", authority_error)

    loop_error = _tool_loop_error(copilot_ctx, "update_and_run_blocks", arguments)
    if loop_error:
        return _diagnosis_repair_tool_error(copilot_ctx, "update_and_run_blocks", loop_error)

    with copilot_span("composition_evidence_precheck", data=_COMPOSITION_EVIDENCE_PRECHECK_TRACE_DATA):
        composition_evidence_error = _update_and_run_blocks_composition_evidence_precheck(
            copilot_ctx,
            workflow_yaml,
            normalized_block_observation_refs,
            block_observation_refs,
        )
    if composition_evidence_error:
        result = {"ok": False, "error": composition_evidence_error}
        record_tool_step_result_for_ctx(copilot_ctx, "update_and_run_blocks", arguments, result)
        _record_diagnosis_repair_contract(
            copilot_ctx,
            source_tool="update_and_run_blocks",
            result=result,
        )
        sanitized = sanitize_tool_result_for_llm("update_and_run_blocks", result)
        return json.dumps(sanitized)

    _clear_pending_browser_interaction_observation(copilot_ctx)

    # Snapshot the prior workflow definition BEFORE _update_workflow saves
    # the new one — we need the pre-update state to diff against.
    prior_definition = await _get_prior_workflow_definition(copilot_ctx)

    # Step 1: Update the workflow
    with copilot_span("update_workflow", data={"yaml_length": len(workflow_yaml)}):
        update_result = await _update_workflow(
            {
                "workflow_yaml": workflow_yaml,
                "block_observation_refs": normalized_block_observation_refs,
                "raw_block_observation_refs": block_observation_refs,
                "code_artifact_metadata": serialized_code_artifact_metadata,
                "raw_code_artifact_metadata": code_artifact_metadata,
            },
            copilot_ctx,
            allow_missing_credentials=skip_run_after_update,
        )
        _record_workflow_update_result(copilot_ctx, update_result, prior_definition)

    if not update_result.get("ok"):
        record_tool_step_result_for_ctx(copilot_ctx, "update_and_run_blocks", arguments, update_result)
        _record_diagnosis_repair_contract(
            copilot_ctx,
            source_tool="update_and_run_blocks",
            result=update_result,
        )
        sanitized = sanitize_tool_result_for_llm("update_workflow", update_result)
        return json.dumps(sanitized)

    coverage_error = _pre_run_workflow_coverage_error(copilot_ctx)
    if coverage_error:
        user_facing_summary = (
            "Workflow draft saved; I still need to add the remaining requested actions before testing it."
        )
        result = {
            "ok": False,
            "error": coverage_error,
            "data": {
                "block_count": copilot_ctx.last_update_block_count,
                "workflow_updated": True,
                "workflow_run_skipped": True,
                "control_signal": {
                    "kind": "intermediate_success",
                    "user_facing_summary": user_facing_summary,
                },
                "user_facing_summary": user_facing_summary,
            },
        }
        record_tool_step_result_for_ctx(copilot_ctx, "update_and_run_blocks", arguments, result)
        _record_diagnosis_repair_contract(
            copilot_ctx,
            source_tool="update_and_run_blocks",
            result=result,
            workflow_updated=True,
        )
        return json.dumps(result)

    if skip_run_after_update:
        skip_message = "Skipped test run: required credentials are not configured."
        skip_result = {
            "ok": True,
            "message": skip_message,
            "data": {
                "block_count": copilot_ctx.last_update_block_count,
                "workflow_updated": True,
                "skipped_run": True,
                "skip_reason": "workflow_credential_inputs_unbound",
            },
        }
        record_tool_step_result_for_ctx(copilot_ctx, "update_and_run_blocks", arguments, skip_result)
        _record_diagnosis_repair_contract(
            copilot_ctx,
            source_tool="update_and_run_blocks",
            result=skip_result,
            workflow_updated=True,
        )
        LOG.info(
            "update_and_run_blocks skipped run on unbound credential workflow inputs",
            workflow_permanent_id=copilot_ctx.workflow_permanent_id,
        )
        return json.dumps(skip_result)

    # Step 2: Compute frontier and run the blocks.
    new_definition = None
    if copilot_ctx.last_workflow is not None:
        new_definition = getattr(copilot_ctx.last_workflow, "workflow_definition", None)

    labels_to_execute, block_outputs_to_seed, frontier_start_label = _plan_frontier(
        copilot_ctx, block_labels, prior_definition, new_definition
    )
    frontier_error = _frontier_run_size_error(copilot_ctx, block_labels, labels_to_execute, new_definition)
    if frontier_error:
        result = _frontier_run_size_result(frontier_error, block_labels, labels_to_execute)
        data = result.get("data")
        if isinstance(data, dict):
            data["workflow_updated"] = True
        record_tool_step_result_for_ctx(copilot_ctx, "update_and_run_blocks", arguments, result)
        _record_diagnosis_repair_contract(
            copilot_ctx,
            source_tool="update_and_run_blocks",
            result=result,
            workflow_updated=True,
        )
        return json.dumps(result)

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
        completion_verification = await _maybe_run_completion_verification(copilot_ctx, run_result, handler_start)
        _record_run_blocks_result(copilot_ctx, run_result, completion_verification=completion_verification)
        tool_visible_result = _tool_visible_result_after_completion_verification(
            copilot_ctx,
            run_result,
            completion_verification,
        )
        record_tool_step_result_for_ctx(copilot_ctx, "update_and_run_blocks", arguments, tool_visible_result)
        _record_diagnosis_repair_contract(
            copilot_ctx,
            source_tool="update_and_run_blocks",
            result=tool_visible_result,
            workflow_updated=True,
        )
        enqueue_screenshot_from_result(copilot_ctx, run_result)
        if run_result.get("ok"):
            advance_to_testing(copilot_ctx)

    sanitized = sanitize_tool_result_for_llm("run_blocks_and_collect_debug", tool_visible_result)
    return json.dumps(sanitized)


# Build-time entrypoint discovery: navigates and reads pages, returns a
# candidate URL into the agent's context. Never mutates workflow YAML.
# Available only during INITIAL / DISCOVERING phases.

_DISCOVERY_PER_CHAT_BUDGET = 3
_DISCOVERY_PER_TURN_BUDGET = 1
_COMPOSITION_INSPECTION_PER_CHAT_BUDGET = 6
_COMPOSITION_INSPECTION_PER_TURN_BUDGET = 4
_DISCOVERY_WALL_CLOCK_SECONDS = 60.0
_DISCOVERY_STEP_CAP = 8
_DISCOVERY_EVIDENCE_TRAIL_MAX = 8
_DISCOVERY_CANDIDATE_FORM_FIELDS_MAX = 10
_DISCOVERY_HTML_BYTES_MAX = 200_000
_DISCOVERY_CONCRETE_HOMEPAGE_CONFIDENCE = 0.6
_COMPOSITION_VISUAL_SUMMARY_TIMEOUT_SECONDS = 10.0
_COMPOSITION_VISUAL_SUMMARY_PROMPT_NAME = "workflow-copilot-page-evidence-vision"

_DISCOVERY_URL_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
# host + optional path/query/fragment — handles `example.com/login`,
# `the-internet.herokuapp.com/tables?x=y`, `site.com?q=x`, `site.com#frag`.
_DISCOVERY_DOMAIN_WITH_PATH_RE = re.compile(
    r"^[a-z0-9-]+(\.[a-z]{2,})+([/?#][^\s]*)?$",
    re.IGNORECASE,
)
_DISCOVERY_BARE_WORD_RE = re.compile(r"^[a-z0-9-]{2,32}$", re.IGNORECASE)
_DISCOVERY_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_DISCOVERY_CANDIDATE_EVIDENCE_STOPWORDS = frozenset({"a", "an", "and", "for", "in", "of", "on", "or", "the", "to"})
_DISCOVERY_LOGIN_TITLE_RE = re.compile(r"\b(sign\s*in|log\s*in|login)\b", re.IGNORECASE)
_DISCOVERY_PASSWORD_INPUT_RE = re.compile(
    r"<input[^>]*type\s*=\s*[\"']password[\"']",
    re.IGNORECASE,
)
_DISCOVERY_ANTI_BOT_PATTERNS = (
    "just a moment",
    "captcha",
    "challenge",
    "turnstile",
    "cf-turnstile",
    "human-verification",
    "human verification",
    "verify you are human",
    "access denied",
    "are you a robot",
)


def _resolve_discovery_entry_url(site_or_url: str) -> tuple[str | None, str]:
    """Resolve the user-supplied site name/URL into a navigable URL.

    Returns ``(resolved_url, kind)`` where ``kind`` is one of:
    ``url`` / ``domain`` / ``word`` / ``unresolved``.
    """
    token = (site_or_url or "").strip()
    if not token:
        return None, "unresolved"
    if _DISCOVERY_URL_SCHEME_RE.match(token):
        return token, "url"
    if _DISCOVERY_DOMAIN_WITH_PATH_RE.match(token):
        return f"https://{token}", "domain"
    if _DISCOVERY_BARE_WORD_RE.match(token):
        return f"https://www.{token.lower()}.com", "word"
    return None, "unresolved"


def _concrete_homepage_entrypoint(entry_url: str | None, kind: str) -> str | None:
    if kind not in {"domain", "url"} or not entry_url:
        return None
    try:
        parsed = urlparse(entry_url)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/"


def _discovery_anchor_score(
    anchor_text: str,
    anchor_title: str,
    href_path: str,
    intent_tokens: set[str],
) -> int:
    """Count intent tokens that appear as substrings of the combined anchor text.

    Substring (not exact-token) matching handles ``sort`` ↔ ``sortable`` and
    ``table`` ↔ ``tables`` without a full stemmer.
    """
    if not intent_tokens:
        return 0
    combined = f"{anchor_text} {anchor_title} {href_path}".lower()
    return sum(1 for token in intent_tokens if token in combined)


def _discovery_title_score(page_title: str, intent_tokens: set[str]) -> int:
    if not intent_tokens or not page_title:
        return 0
    lowered = page_title.lower()
    return sum(1 for token in intent_tokens if token in lowered)


def _discovery_candidate_evidence_tokens(intent_tokens: set[str]) -> set[str]:
    return {
        token
        for token in intent_tokens
        if len(token) > 2 and token.lower() not in _DISCOVERY_CANDIDATE_EVIDENCE_STOPWORDS
    }


def _discovery_detect_login_wall(html: str, page_title: str) -> bool:
    if _DISCOVERY_LOGIN_TITLE_RE.search(page_title or ""):
        return True
    return bool(_DISCOVERY_PASSWORD_INPUT_RE.search(html or ""))


def _discovery_detect_anti_bot(html: str, page_title: str) -> bool:
    lowered_title = (page_title or "").lower()
    lowered_html = (html or "")[:_DISCOVERY_HTML_BYTES_MAX].lower()
    return any(pat in lowered_title or pat in lowered_html for pat in _DISCOVERY_ANTI_BOT_PATTERNS)


def _discovery_build_result(
    *,
    candidate_url: str | None,
    candidate_form_fields: list[dict[str, Any]],
    evidence_trail: list[dict[str, Any]],
    confidence: float,
    failure_reason: str | None,
    ok: bool = True,
    error: str | None = None,
) -> dict[str, Any]:
    """Shape a `discover_workflow_entrypoint` result envelope.

    Convention: ``ok=True`` for any *completed* walk — including controlled
    outcomes that report a ``failure_reason`` and ``candidate_url=None``.
    ``ok=False`` is reserved for actual tool errors (MCP unavailable, browser
    boot failure, internal exception). Matches the existing copilot
    ``_request_policy_tool_error`` convention so the eval harness counts a
    controlled failure as a successful tool call.
    """
    return {
        "ok": ok,
        "data": {
            "candidate_url": candidate_url,
            "candidate_form_fields": candidate_form_fields[:_DISCOVERY_CANDIDATE_FORM_FIELDS_MAX],
            "evidence_trail": evidence_trail[:_DISCOVERY_EVIDENCE_TRAIL_MAX],
            "confidence": float(confidence),
            "failure_reason": failure_reason,
        },
        "error": error,
    }


def _record_discovery_resolution_on_ctx(ctx: Any, result: Mapping[str, Any]) -> None:
    data_payload = result.get("data")
    data: Mapping[str, Any] = data_payload if isinstance(data_payload, Mapping) else {}
    candidate_url = data.get("candidate_url")
    failure_reason = data.get("failure_reason")
    if isinstance(candidate_url, str) and candidate_url:
        prior_candidate_url = getattr(ctx, "resolved_discovery_entrypoint_url", None)
        ctx.resolved_discovery_entrypoint_url = candidate_url
        ctx.resolved_discovery_failure_reason = (
            failure_reason if isinstance(failure_reason, str) and failure_reason else None
        )
        if prior_candidate_url != candidate_url:
            ctx.resolved_discovery_entrypoint_inspection_baseline = int(
                getattr(ctx, "page_inspection_calls_this_turn", 0) or 0
            )
            ctx.discovery_entrypoint_url_question_nudge_count = 0
    # Prior successful candidates remain authoritative over later no-candidate failures.
    elif not getattr(ctx, "resolved_discovery_entrypoint_url", None):
        # No prior candidate and no new one: clear URL state while recording the failure.
        ctx.resolved_discovery_entrypoint_url = None
        ctx.resolved_discovery_failure_reason = (
            failure_reason if isinstance(failure_reason, str) and failure_reason else None
        )
        ctx.resolved_discovery_entrypoint_inspection_baseline = 0
    try:
        current_span = otel_trace.get_current_span()
        if ctx.resolved_discovery_entrypoint_url is not None:
            current_span.set_attribute("copilot.discovery_candidate_url", ctx.resolved_discovery_entrypoint_url)
        if ctx.resolved_discovery_failure_reason is not None:
            current_span.set_attribute("copilot.discovery_failure_reason", ctx.resolved_discovery_failure_reason)
    except Exception:
        LOG.debug("Unable to set discovery resolution span attributes", exc_info=True)


def _discovery_parse_html(html: str) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    """Parse HTML for page title, link anchors, and form-field metadata.

    Uses BeautifulSoup if available (a transitive Skyvern dep). Falls back to
    empty results if not — discovery degrades gracefully rather than crashing.
    """
    if BeautifulSoup is None:
        return "", [], []

    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return "", [], []

    title_text = ""
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        title_text = title_tag.string.strip()
    h1_tag = soup.find("h1")
    if h1_tag:
        h1_text = h1_tag.get_text(strip=True)
        if h1_text:
            title_text = f"{title_text} {h1_text}".strip()

    anchors: list[dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        text = a.get_text(" ", strip=True)
        anchors.append(
            {
                "href": href,
                "text": text[:240],
                "title": (a.get("title") or "")[:240],
            }
        )

    form_fields: list[dict[str, str]] = []
    form = soup.find("form")
    if form is not None:
        for inp in form.find_all(["input", "select", "textarea"]):
            field_type = inp.get("type", inp.name) or "text"
            if field_type.lower() in {"hidden", "submit", "button"}:
                continue
            field_name = inp.get("name") or inp.get("id") or ""
            label_text = ""
            label_id = inp.get("id")
            if label_id:
                label_tag = soup.find("label", attrs={"for": label_id})
                if label_tag is not None:
                    label_text = label_tag.get_text(" ", strip=True)
            form_fields.append(
                {
                    "name": field_name[:120],
                    "label": label_text[:240],
                    "type": str(field_type)[:40],
                    "value_hint": (inp.get("placeholder") or "")[:240],
                }
            )

    return title_text, anchors, form_fields


def _discovery_resolve_href(base_url: str, href: str) -> str | None:
    try:
        absolute = urljoin(base_url, href)
    except Exception:
        return None
    parsed_abs = urlparse(absolute)
    parsed_base = urlparse(base_url)
    if parsed_abs.scheme not in {"http", "https"}:
        return None
    # Same-origin only — discovery does not follow cross-origin links to keep
    # the entrypoint search bounded to the user's named site.
    if parsed_abs.netloc and parsed_base.netloc and parsed_abs.netloc != parsed_base.netloc:
        return None
    return absolute


def _discovery_origin_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/"


def _discovery_should_retry_from_origin(entry_url: str, current_url: str) -> bool:
    try:
        parsed_entry = urlparse(entry_url)
        parsed_current = urlparse(current_url)
    except Exception:
        return False
    if parsed_current.scheme not in {"http", "https"} or not parsed_current.netloc:
        return False
    if parsed_entry.netloc and parsed_entry.netloc != parsed_current.netloc:
        return False
    return bool(parsed_current.path not in {"", "/"} or parsed_current.query)


def _discovery_anchor_selector(anchor: dict[str, str]) -> str | None:
    href = (anchor.get("href") or "").strip()
    if not href or any(char in href for char in {'"', "\\", "\n", "\r"}):
        return None
    return f'a[href="{href}"]'


# Per-call timeout for each MCP primitive inside the discovery walker. The
# walker also checks the cumulative 60s wall clock between steps, but without
# a per-call cap a single hung navigate or get_html could block past the
# cumulative cap (cumulative is only checked between awaits).
_DISCOVERY_PER_CALL_TIMEOUT_SECONDS = 20.0
_DISCOVERY_NAVIGATION_FALLBACK_CONFIDENCE = 0.2


async def _discovery_navigate(
    ctx: CopilotContext,
    url: str,
    *,
    wait_until: str | None = None,
    timeout_seconds: float = _DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return {"ok": False, "error": "discovery MCP server not attached to context"}
    nav_args: dict[str, Any] = {"url": url}
    cap = timeout_seconds
    if wait_until:
        # `load` waits for every resource (analytics/marketing beacons on heavy
        # commerce pages keep it pending past the cap, so the navigate aborts before
        # any HTML is captured). `domcontentloaded` returns once the server-rendered
        # DOM is parsed — the forms/links are already present — and the recapture
        # loop settles anything still hydrating.
        nav_args["wait_until"] = wait_until
        nav_args["timeout"] = int(timeout_seconds * 1000)
        cap = timeout_seconds + 5
    try:
        return await asyncio.wait_for(
            server.call_internal_tool("skyvern_navigate", nav_args),
            timeout=cap,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"skyvern_navigate timed out after {timeout_seconds:g}s"}


async def _discovery_click_anchor(ctx: CopilotContext, anchor: dict[str, str]) -> dict[str, Any]:
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return {"ok": False, "error": "discovery MCP server not attached to context"}
    selector = _discovery_anchor_selector(anchor)
    if selector is None:
        return {"ok": False, "error": "anchor href could not be converted to a bounded CSS selector"}
    try:
        return await asyncio.wait_for(
            # call_internal_tool bypasses the schema overlays, so selector_mode="direct" must be
            # passed explicitly here (it is not picked up from the overlay's forced_args).
            server.call_internal_tool("skyvern_click", {"selector": selector, "selector_mode": "direct"}),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"skyvern_click timed out after {_DISCOVERY_PER_CALL_TIMEOUT_SECONDS:g}s"}


async def _discovery_get_html(ctx: CopilotContext) -> dict[str, Any]:
    """Read the full page body. ``skyvern_get_html`` requires a selector arg;
    pass ``body`` so the walker receives the full document body. Without this
    the raw MCP call fails validation since the inspection tool has a
    required positional ``selector``.
    """
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return {"ok": False, "error": "discovery MCP server not attached to context"}
    try:
        return await asyncio.wait_for(
            server.call_internal_tool("skyvern_get_html", {"selector": "body"}),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"skyvern_get_html timed out after {_DISCOVERY_PER_CALL_TIMEOUT_SECONDS:g}s"}


async def _composition_get_screenshot(ctx: CopilotContext) -> dict[str, Any]:
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return {"ok": False, "error": "discovery MCP server not attached to context"}
    try:
        return await asyncio.wait_for(
            server.call_internal_tool("skyvern_screenshot", {"inline": True}),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"skyvern_screenshot timed out after {_DISCOVERY_PER_CALL_TIMEOUT_SECONDS:g}s"}


def _discovery_extract_html_payload(result: dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("html", "outer_html", "text", "content"):
            value = data.get(key)
            if isinstance(value, str):
                return value
    return ""


def _discovery_extract_current_url(result: dict[str, Any], fallback: str) -> str:
    data = result.get("data")
    if isinstance(data, dict):
        url = data.get("url") or data.get("current_url")
        if isinstance(url, str) and url:
            return url
    return fallback


def _composition_extract_screenshot_b64(result: dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("screenshot_base64", "data", "image_base64"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _composition_visual_prompt(evidence: dict[str, Any]) -> str:
    context = {
        "page_title": evidence.get("page_title") or "",
        "current_url": evidence.get("current_url") or "",
        "anti_bot_indicators": evidence.get("anti_bot_indicators") or [],
        "challenge_state": evidence.get("challenge_state") or {},
        "form_count": len(evidence.get("forms") or []),
        "result_container_count": len(evidence.get("result_containers") or []),
        "page_obstruction_count": len(evidence.get("page_obstructions") or []),
        "visual_obstruction_candidate_count": len(evidence.get("visual_obstruction_candidates") or []),
        "schema_empty_page": evidence.get("schema_empty_page") is True,
    }
    return (
        "Summarize this screenshot for Workflow Copilot build-time page evidence. "
        "Return JSON only with keys: summary, challenge_detected, challenge_kind, "
        "challenge_location, submit_blocked, blocked_submit_controls, empty_page_visible, "
        "loading_state_visible, page_obstruction_detected, obstruction_kind, "
        "obstruction_location, underlying_page_blocked, visible_dismiss_controls, omissions. "
        "In summary, include the visible page state that would help verify an end-state outcome, "
        "such as cart items, "
        "record rows, visible identifiers, quantities, statuses, prices, confirmations, search results, "
        "or selected values when legible. Also note visible anti-bot or human-verification state, "
        "whether it appears to gate a submit/search control, whether any visible artificial barrier "
        "mechanically blocks the underlying page, and where it appears relative to the page controls. "
        "Do not include raw DOM, code, selectors, personal data, or workflow instructions. "
        "If no challenge is visible, set challenge_detected to false and submit_blocked to false. "
        "If no page obstruction is visible, set page_obstruction_detected to false. "
        "If DOM context shows a schema-empty page, set empty_page_visible to true only when the "
        "screenshot shows a settled page with no visible forms, controls, result data, challenge, "
        "or loading/progress state; set loading_state_visible to true when the page appears to be "
        "waiting, loading, redirecting, or still rendering.\n\n"
        f"DOM evidence context:\n{json.dumps(context, sort_keys=True)}"
    )


async def _composition_visual_handler(ctx: CopilotContext) -> Any | None:
    return await resolve_fast_copilot_handler(
        getattr(ctx, "workflow_permanent_id", None),
        getattr(ctx, "organization_id", None),
    )


def _normalize_visual_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    summary = value.get("summary")
    challenge_detected = value.get("challenge_detected")
    challenge_kind = value.get("challenge_kind")
    challenge_location = value.get("challenge_location")
    submit_blocked = value.get("submit_blocked")
    blocked_submit_controls = value.get("blocked_submit_controls")
    empty_page_visible = value.get("empty_page_visible")
    loading_state_visible = value.get("loading_state_visible")
    page_obstruction_detected = value.get("page_obstruction_detected")
    obstruction_kind = value.get("obstruction_kind")
    obstruction_location = value.get("obstruction_location")
    underlying_page_blocked = value.get("underlying_page_blocked")
    visible_dismiss_controls = value.get("visible_dismiss_controls")
    omissions = value.get("omissions")
    return {
        "summary": summary if isinstance(summary, str) else "",
        "challenge_detected": challenge_detected if isinstance(challenge_detected, bool) else None,
        "challenge_kind": challenge_kind if isinstance(challenge_kind, str) else "",
        "challenge_location": challenge_location if isinstance(challenge_location, str) else "",
        "submit_blocked": submit_blocked if isinstance(submit_blocked, bool) else None,
        "empty_page_visible": empty_page_visible if isinstance(empty_page_visible, bool) else None,
        "loading_state_visible": loading_state_visible if isinstance(loading_state_visible, bool) else None,
        "page_obstruction_detected": page_obstruction_detected if isinstance(page_obstruction_detected, bool) else None,
        "obstruction_kind": obstruction_kind if isinstance(obstruction_kind, str) else "",
        "obstruction_location": obstruction_location if isinstance(obstruction_location, str) else "",
        "underlying_page_blocked": underlying_page_blocked if isinstance(underlying_page_blocked, bool) else None,
        "blocked_submit_controls": [item for item in blocked_submit_controls if isinstance(item, str)]
        if isinstance(blocked_submit_controls, list)
        else [],
        "visible_dismiss_controls": [item for item in visible_dismiss_controls if isinstance(item, str)]
        if isinstance(visible_dismiss_controls, list)
        else [],
        "omissions": [item for item in omissions if isinstance(item, str)] if isinstance(omissions, list) else [],
    }


async def _composition_summarize_screenshot(
    ctx: CopilotContext,
    *,
    evidence: dict[str, Any],
    screenshot_b64: str,
) -> tuple[dict[str, Any] | None, str | None]:
    handler = await _composition_visual_handler(ctx)
    if handler is None:
        return None, "workflow copilot LLM handler is not configured"
    try:
        screenshot_bytes = base64.b64decode(screenshot_b64, validate=True)
    except Exception:
        return None, "screenshot payload was not valid base64"
    try:
        response = await asyncio.wait_for(
            handler(
                prompt=_composition_visual_prompt(evidence),
                prompt_name=_COMPOSITION_VISUAL_SUMMARY_PROMPT_NAME,
                screenshots=[screenshot_bytes],
                organization_id=getattr(ctx, "organization_id", None),
                force_dict=True,
            ),
            timeout=_COMPOSITION_VISUAL_SUMMARY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return None, f"visual summary timed out after {_COMPOSITION_VISUAL_SUMMARY_TIMEOUT_SECONDS:g}s"
    except Exception as exc:
        LOG.warning("Composition screenshot visual summary failed", error=str(exc), exc_info=True)
        return None, str(exc)
    normalized = _normalize_visual_summary(response)
    if normalized is None:
        return None, "visual summary response was not a JSON object"
    return normalized, None


async def _augment_composition_evidence_with_visual_fallback(
    ctx: CopilotContext,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    screenshot_result = await _composition_get_screenshot(ctx)
    if not screenshot_result.get("ok"):
        return _composition_add_evidence_omission(
            evidence,
            f"screenshot_capture_failed: {screenshot_result.get('error', 'unknown')}",
        )
    screenshot_b64 = _composition_extract_screenshot_b64(screenshot_result)
    visual_summary, visual_error = await _composition_summarize_screenshot(
        ctx,
        evidence=evidence,
        screenshot_b64=screenshot_b64,
    )
    return merge_visual_composition_evidence(evidence, visual_summary=visual_summary, visual_error=visual_error)


def _composition_add_evidence_omission(evidence: dict[str, Any], message: str) -> dict[str, Any]:
    merged = dict(evidence)
    omissions = [item for item in merged.get("visual_evidence_omissions") or [] if isinstance(item, str)]
    if message:
        omissions.append(message[:160])
    merged["visual_evidence_omissions"] = list(dict.fromkeys(omissions))[:5]
    return merged


def _composition_add_inspection_warning(evidence: dict[str, Any], message: str) -> dict[str, Any]:
    merged = dict(evidence)
    warnings = [item for item in merged.get("inspection_warnings") or [] if isinstance(item, str)]
    if message:
        warnings.append(message[:240])
    merged["inspection_warnings"] = list(dict.fromkeys(warnings))[:5]
    return merged


async def _composition_evidence_after_navigation_failure(
    ctx: CopilotContext,
    *,
    inspected_url: str,
    navigation_error: str,
) -> dict[str, Any] | None:
    current_url, _ = await _fallback_page_info(ctx)
    current_url = current_url or inspected_url
    structured = await _composition_get_structured_evidence(ctx, inspected_url=inspected_url, current_url=current_url)
    if structured is not None and has_bounded_page_schema(structured):
        evidence = _composition_add_inspection_warning(
            structured,
            f"navigation_error_before_html_capture: {navigation_error}",
        )
        if page_evidence_needs_visual_fallback(evidence):
            evidence = await _augment_composition_evidence_with_visual_fallback(ctx, evidence)
        return evidence
    # Same size-cap survival as the success path: a heavy page that rendered before the nav
    # error still parses via the stripped-body evaluate instead of yielding hollow evidence.
    html, html_error, html_truncated, _ = await _composition_get_html(ctx)
    if html_error is None:
        evidence = parse_composition_html(html, inspected_url=inspected_url, current_url=current_url)
        evidence = _composition_add_inspection_warning(
            evidence,
            f"navigation_error_before_html_capture: {navigation_error}",
        )
        if html_truncated:
            evidence = _composition_add_inspection_warning(evidence, "html_sliced_at_cap")
        evidence = await _augment_composition_evidence_with_computed_obstruction_candidates(ctx, evidence)
        if page_evidence_needs_visual_fallback(evidence):
            evidence = await _augment_composition_evidence_with_visual_fallback(ctx, evidence)
        return evidence

    evidence = parse_composition_html("", inspected_url=inspected_url, current_url=current_url)
    evidence = _composition_add_inspection_warning(
        evidence,
        f"navigation_error_before_evidence_capture: {navigation_error}",
    )
    evidence = _composition_add_inspection_warning(
        evidence,
        f"html_capture_failed_after_navigation_error: {html_error}",
    )
    evidence = await _augment_composition_evidence_with_visual_fallback(ctx, evidence)
    return evidence if evidence.get("screenshot_used") else None


# Large enough for long single-turn reconnaissance: block_observation_refs are
# chosen after scouting, so this cap must stay above the citation window a turn
# can realistically compose against. Entries carry bounded parse summaries, not
# raw HTML or screenshots.
_MAX_FLOW_EVIDENCE = 64


def _inspection_reached_via(*, use_current_page: bool, post_run: bool, earned_interaction: bool) -> str:
    """How the just-inspected state was reached, for the flow-evidence trajectory.

    A target_url inspection navigates there itself ("navigate"); a post-run
    current-page inspection observes the page the run left behind ("post_run"); a
    normal current-page inspection counts as an interaction only when a successful
    browser action immediately earned that credit.
    """
    if not use_current_page:
        return "navigate"
    if post_run:
        return "post_run"
    return "interaction" if earned_interaction else "current_page"


def _append_flow_evidence(copilot_ctx: Any, evidence: dict[str, Any], *, reached_via: str) -> int | None:
    """Append a typed entry to the bounded flow-evidence trajectory (SKY-10562).

    One entry per scouted page: the page-evidence packet plus how it was reached
    and whether bounded schema was captured. Feeds the per-acted-page composition
    gate and the cross-turn observed-page summary; never written into the YAML.
    """
    trajectory = getattr(copilot_ctx, "flow_evidence", None)
    if not isinstance(trajectory, list):
        return None
    prior_steps = [entry.get("step") for entry in trajectory if isinstance(entry, dict)]
    step = (
        max((value for value in prior_steps if isinstance(value, int) and not isinstance(value, bool)), default=-1) + 1
    )
    trajectory.append(
        {
            "evidence": evidence,
            "reached_via": reached_via,
            "had_bounded_schema": has_bounded_page_schema(evidence),
            "step": step,
        }
    )
    if len(trajectory) > _MAX_FLOW_EVIDENCE:
        overflow_entry_count = len(trajectory) - _MAX_FLOW_EVIDENCE
        LOG.warning(
            "copilot_flow_evidence_evicted",
            overflow_entry_count=overflow_entry_count,
            max_flow_evidence=_MAX_FLOW_EVIDENCE,
            retained_window_size=_MAX_FLOW_EVIDENCE,
            latest_step=step,
        )
        del trajectory[:-_MAX_FLOW_EVIDENCE]
    return step


def _latest_interaction_reached_flow_evidence(copilot_ctx: Any) -> tuple[int, str, dict[str, Any]] | None:
    trajectory = getattr(copilot_ctx, "flow_evidence", None)
    if not isinstance(trajectory, list):
        return None
    for entry in reversed(trajectory):
        if not isinstance(entry, dict):
            continue
        reached_via = str(entry.get("reached_via") or "")
        if reached_via not in {"interaction", "post_run"}:
            continue
        evidence = entry.get("evidence")
        step = entry.get("step")
        if isinstance(step, bool) or not isinstance(step, int) or not isinstance(evidence, dict):
            continue
        if not has_bounded_page_schema(evidence):
            continue
        observed_url = _composition_evidence_page_url(evidence)
        if observed_url:
            return step, observed_url, evidence
    return None


def _non_current_inspection_regression_error(copilot_ctx: Any, *, entry_url: str) -> dict[str, Any] | None:
    latest = _latest_interaction_reached_flow_evidence(copilot_ctx)
    if latest is None:
        return None
    observation_step, observed_url, _ = latest
    if _same_page_ignoring_fragment(observed_url, entry_url):
        return None
    return {
        "ok": False,
        "data": {
            "current_url": observed_url,
            "observation_step": observation_step,
        },
        "error": (
            "inspect_page_for_composition would navigate away from the latest interaction-reached page "
            f'({observed_url}). Use inspect_page_for_composition(target_url="current_page") to inspect '
            "the live page, or compose from the existing page evidence and pass observation_step "
            f"{observation_step} in block_observation_refs for blocks that act on that reached page."
        ),
    }


def _page_inspection_budget_error(copilot_ctx: Any, *, scope: Literal["turn", "chat"]) -> str:
    scope_label = "turn" if scope == "turn" else "chat"
    return (
        f"inspect_page_for_composition reached the page-inspection budget for this {scope_label}. "
        "This is not evidence that scouting is complete. Use evaluate, get_browser_screenshot, or a browser "
        "action on the current page to determine whether the goal is already satisfied, whether progress is still "
        "possible, or whether a real blocker exists. Do not author downstream result, extraction, or confirmation "
        "blocks unless the existing evidence already shows the page state those blocks will act on."
    )


_COMPOSITION_HOLLOW_RECAPTURE_RETRIES = 2
_COMPOSITION_HOLLOW_RECAPTURE_DELAY_SECONDS = 2.5
# The composition inspect navigates with `domcontentloaded`, so a heavier cap than
# the discovery walker's is safe — the navigate returns at DOM parse, well before
# this ceiling, and only a genuinely stuck load reaches it.
_COMPOSITION_NAVIGATE_TIMEOUT_SECONDS = 30.0


async def _composition_get_stripped_html(copilot_ctx: Any) -> tuple[str | None, bool]:
    """Return (stripped_body_html, truncated). truncated is True when the expression sliced
    the body at the cap, so the tail (below-fold forms/controls) is missing from the evidence."""
    server = getattr(copilot_ctx, "discovery_mcp_server", None)
    if server is None:
        return None, False
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool("skyvern_evaluate", {"expression": _COMPOSITION_STRIPPED_HTML_EXPRESSION}),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except Exception:
        return None, False
    if not isinstance(result, dict) or not result.get("ok"):
        return None, False
    value = (result.get("data") or {}).get("result")
    if not isinstance(value, str):
        return None, False
    return value, len(value) >= _COMPOSITION_STRIPPED_HTML_MAX_CHARS


def _normalize_visual_obstruction_candidates(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    candidates: list[dict[str, Any]] = []
    for item in value:
        if len(candidates) >= 5:
            break
        if not isinstance(item, dict):
            continue
        position = item.get("position")
        coverage = item.get("coverage")
        if position not in {"fixed", "sticky"} or coverage != "viewport":
            continue
        candidates.append(
            {
                "source": "computed_style",
                "position": position,
                "coverage": "viewport",
                "has_visible_controls": item.get("has_visible_controls") is True,
            }
        )
    return candidates


def _merge_visual_obstruction_candidates(
    evidence: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if not candidates:
        return evidence
    merged = dict(evidence)
    existing = [item for item in merged.get("visual_obstruction_candidates") or [] if isinstance(item, dict)]
    for candidate in candidates:
        if len(existing) >= 5:
            break
        if candidate not in existing:
            existing.append(candidate)
    merged["visual_obstruction_candidates"] = existing[:5]
    return merged


async def _composition_get_computed_visual_obstruction_candidates(copilot_ctx: Any) -> list[dict[str, Any]]:
    server = getattr(copilot_ctx, "discovery_mcp_server", None)
    if server is None:
        return []
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool(
                "skyvern_evaluate",
                {"expression": _COMPOSITION_VISUAL_OBSTRUCTION_CANDIDATES_EXPRESSION},
            ),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except Exception:
        return []
    if not isinstance(result, dict) or not result.get("ok"):
        return []
    value = (result.get("data") or {}).get("result")
    return _normalize_visual_obstruction_candidates(value)


async def _augment_composition_evidence_with_computed_obstruction_candidates(
    copilot_ctx: Any,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if page_evidence_needs_visual_fallback(evidence) or not has_bounded_page_schema(evidence):
        return evidence
    candidates = await _composition_get_computed_visual_obstruction_candidates(copilot_ctx)
    return _merge_visual_obstruction_candidates(evidence, candidates)


async def _composition_get_html(copilot_ctx: Any, *, skip_raw: bool = False) -> tuple[str, str | None, bool, bool]:
    """Return body HTML for composition parsing, surviving the MCP response size cap.

    `skyvern_get_html("body")` is the fast path, but the shared size cap DROPS the
    payload (no `html` field, just truncation metadata) when the serialized body
    exceeds the limit — heavy commerce pages routinely do, so the inspector would
    parse an empty string and report hollow evidence. On an empty/capped read, fall
    back to an `evaluate` that returns the body with script/style/svg/etc. stripped
    and length-bounded; that fits under the cap while preserving the form/link
    structure. Returns (html, error, truncated, used_stripped): error is set only on
    a hard read failure; truncated is True when the stripped fallback was sliced at
    the cap; used_stripped is True when the bounded read was the source (raw skipped
    or cap-dropped). `skip_raw` goes straight to the stripped read so a caller that
    has already seen the raw serialization get cap-dropped for this page need not
    re-issue it.
    """
    html_result: dict[str, Any] = {}
    if not skip_raw:
        html_result = await _discovery_get_html(copilot_ctx)
        if html_result.get("ok"):
            html = _discovery_extract_html_payload(html_result)
            if html.strip():
                return html, None, False, False
    stripped, truncated = await _composition_get_stripped_html(copilot_ctx)
    if stripped and stripped.strip():
        return stripped, None, truncated, True
    error = html_result.get("error")
    return "", str(error) if error else None, False, True


async def _composition_get_structured_evidence(
    copilot_ctx: Any,
    *,
    inspected_url: str,
    current_url: str,
) -> dict[str, Any] | None:
    """Capture composition evidence via the page-side extractor; None when it can't yield a usable payload."""
    server = getattr(copilot_ctx, "discovery_mcp_server", None)
    if server is None:
        return None
    with copilot_span("composition_structured_extract"):
        try:
            result = await asyncio.wait_for(
                server.call_internal_tool(
                    "skyvern_evaluate", {"expression": _COMPOSITION_STRUCTURED_EVIDENCE_EXPRESSION}
                ),
                timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
            )
        except Exception:
            return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    raw = (result.get("data") or {}).get("result")
    if isinstance(raw, str):
        if len(raw) > _COMPOSITION_STRUCTURED_EVIDENCE_MAX_CHARS:
            return None
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return None
    elif isinstance(raw, dict):
        payload = raw
    else:
        return None
    return parse_composition_structured(payload, inspected_url=inspected_url, current_url=current_url)


async def _capture_composition_evidence(
    copilot_ctx: Any,
    *,
    inspected_url: str,
    current_url: str,
    active_run_terminal_sample: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """Parse composition evidence (cheap extractor first, get_html fallback); html_error is set only on a failed HTML read."""
    evidence: dict[str, Any] | None = None
    html_truncated = False
    used_structured = False
    skip_raw = False
    for attempt in range(_COMPOSITION_HOLLOW_RECAPTURE_RETRIES + 1):
        structured = await _composition_get_structured_evidence(
            copilot_ctx, inspected_url=inspected_url, current_url=current_url
        )
        if structured is not None:
            evidence = structured
            used_structured = True
            if has_bounded_page_schema(evidence):
                break
            if attempt < _COMPOSITION_HOLLOW_RECAPTURE_RETRIES:
                await asyncio.sleep(_COMPOSITION_HOLLOW_RECAPTURE_DELAY_SECONDS)
                continue
        html, html_error, html_truncated, used_stripped = await _composition_get_html(copilot_ctx, skip_raw=skip_raw)
        if html_error is not None:
            if evidence is not None:
                break
            return None, html_error
        # On a heavy page the raw get_html serialization is dropped over the MCP size cap and
        # falls back to the stripped read; once that happens, settle-and-recapture via the
        # stripped path only so a slow page is still retried without re-serializing the full DOM.
        if used_stripped:
            skip_raw = True
        evidence = parse_composition_html(html, inspected_url=inspected_url, current_url=current_url)
        used_structured = False
        if has_bounded_page_schema(evidence):
            break
        if attempt < _COMPOSITION_HOLLOW_RECAPTURE_RETRIES:
            await asyncio.sleep(_COMPOSITION_HOLLOW_RECAPTURE_DELAY_SECONDS)
    if evidence is not None and html_truncated and not used_structured:
        evidence = _composition_add_inspection_warning(evidence, "html_sliced_at_cap")
    # Structured evidence already carries computed obstruction candidates; only the get_html path augments.
    if evidence is not None and not used_structured:
        evidence = await _augment_composition_evidence_with_computed_obstruction_candidates(copilot_ctx, evidence)
    if evidence is not None and (
        page_evidence_needs_visual_fallback(evidence)
        or (active_run_terminal_sample and _active_run_terminal_evidence_needs_visual_fallback(evidence))
        or (evidence.get("schema_empty_page") is True and not has_bounded_page_schema(evidence))
    ):
        evidence = await _augment_composition_evidence_with_visual_fallback(copilot_ctx, evidence)
    return evidence, None


def _normalized_inspect_url(url: str | None) -> str | None:
    """Normalized full URL for strict same-page comparison, or None when not comparable.

    Preserves scheme, the path's trailing slash, query, and fragment so distinct rendered
    states (http vs https, /p vs /p/, ?q=a vs ?q=b, hash-routed SPA states) never collide;
    only netloc case and an empty root path are normalized.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}{query}{fragment}"


def _same_inspect_target(live_url: str | None, target_url: str | None) -> bool:
    """True when the live page is the exact page a URL-target inspect would navigate to.

    Strict full-URL equality, so a different scheme, trailing slash, query, or fragment
    still navigates. Used to skip the re-navigation when the agent is already standing on
    the requested page.
    """
    live_key = _normalized_inspect_url(live_url)
    target_key = _normalized_inspect_url(target_url)
    return live_key is not None and live_key == target_key


async def _inspect_page_for_composition_impl(
    copilot_ctx: Any,
    target_url: str,
) -> dict[str, Any]:
    """Inspect a known target page and store form/search evidence on ctx.

    This is composition context, not workflow YAML. It is intentionally separate
    from `discover_workflow_entrypoint`: discovery answers "which page?";
    inspection answers "what fields and controls are actually on this page?".
    """
    arguments = {"target_url": target_url}
    authority_error = _authority_tool_error(copilot_ctx, "inspect_page_for_composition")
    if authority_error:
        result = {"ok": False, "error": authority_error}
        record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
        return result

    use_current_page = (target_url or "").strip().lower() in _CURRENT_PAGE_INSPECTION_TARGETS
    if not use_current_page:
        _clear_pending_browser_interaction_observation(copilot_ctx)
    bypass_budget_for_post_run_current_page = _allows_post_run_current_page_inspection_budget_bypass(
        copilot_ctx,
        use_current_page=use_current_page,
    )

    entry_url: str
    kind: str
    if use_current_page:
        current_url, _ = await _fallback_page_info(copilot_ctx)
        entry_url = current_url or "current_page"
        kind = "current_page"
    else:
        resolved_entry_url, kind = _resolve_discovery_entry_url(target_url)
        if resolved_entry_url is None:
            result = {
                "ok": False,
                "data": None,
                "error": "inspect_page_for_composition requires a URL, domain with an explicit path, or target_url='current_page'.",
            }
            record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
            return result
        entry_url = resolved_entry_url
        regression_error = _non_current_inspection_regression_error(copilot_ctx, entry_url=entry_url)
        if regression_error is not None:
            record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, regression_error)
            return regression_error

    # Skip re-navigation when the inspect target is the page the browser is already on. A
    # passive client-side redirect can move the browser without a tool, so for a URL target
    # confirm against the live URL; for current_page the live URL is the target by definition.
    if use_current_page:
        inspect_target_url = current_url
        on_target_page = True
    else:
        live_url, _ = await _fallback_page_info(copilot_ctx)
        on_target_page = _same_inspect_target(live_url, entry_url)
        inspect_target_url = live_url if on_target_page else entry_url

    if (
        not bypass_budget_for_post_run_current_page
        and copilot_ctx.page_inspection_calls_this_turn >= _COMPOSITION_INSPECTION_PER_TURN_BUDGET
    ):
        result = {
            "ok": False,
            "data": None,
            "error": _page_inspection_budget_error(copilot_ctx, scope="turn"),
        }
        record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
        return result

    cumulative = copilot_ctx.prior_page_inspection_calls_made + copilot_ctx.page_inspection_calls_this_turn
    if not bypass_budget_for_post_run_current_page and cumulative >= _COMPOSITION_INSPECTION_PER_CHAT_BUDGET:
        result = {
            "ok": False,
            "data": None,
            "error": _page_inspection_budget_error(copilot_ctx, scope="chat"),
        }
        record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
        return result

    evidence = None
    html_error: str | None = None
    with copilot_span(
        "inspect_page_for_composition",
        data={"target_url_kind": kind},
    ):
        if on_target_page:
            # current_page, or a URL target the agent is already on — capture without navigating.
            current_url = inspect_target_url or entry_url
            evidence, html_error = await _capture_composition_evidence(
                copilot_ctx, inspected_url=entry_url, current_url=current_url
            )
        else:
            nav_result = await _discovery_navigate(
                copilot_ctx,
                entry_url,
                wait_until="domcontentloaded",
                timeout_seconds=_COMPOSITION_NAVIGATE_TIMEOUT_SECONDS,
            )
            if not nav_result.get("ok"):
                nav_error = str(nav_result.get("error") or "unknown")
                failure_evidence = await _composition_evidence_after_navigation_failure(
                    copilot_ctx,
                    inspected_url=entry_url,
                    navigation_error=nav_error,
                )
                if failure_evidence is None:
                    result = {
                        "ok": False,
                        "data": None,
                        "error": f"inspect_page_for_composition could not navigate: {nav_error}",
                    }
                    record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
                    return result
                evidence = failure_evidence
                current_url = str(evidence.get("current_url") or entry_url)
            else:
                current_url = _discovery_extract_current_url(nav_result, entry_url)
                evidence, html_error = await _capture_composition_evidence(
                    copilot_ctx, inspected_url=entry_url, current_url=current_url
                )

    if html_error is not None:
        result = {
            "ok": False,
            "data": None,
            "error": f"inspect_page_for_composition could not read page HTML: {html_error}",
        }
        record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
        return result

    if evidence is None:
        result = {
            "ok": False,
            "data": None,
            "error": "inspect_page_for_composition could not read page HTML.",
        }
        record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
        return result

    run_id = getattr(copilot_ctx, "last_run_blocks_workflow_run_id", None)
    if isinstance(run_id, str) and run_id:
        evidence = {
            **evidence,
            "workflow_run_id": run_id,
            "observed_after_workflow_run": True,
        }
        _mark_post_run_page_observed(copilot_ctx, source_tool="inspect_page_for_composition", url=current_url)
        page_title = evidence.get("page_title")
        if isinstance(page_title, str) and page_title:
            _workflow_verification_evidence(copilot_ctx).page_title = page_title[:160]

    if not bypass_budget_for_post_run_current_page:
        copilot_ctx.page_inspection_calls_this_turn += 1
    if bypass_budget_for_post_run_current_page:
        copilot_ctx.post_run_current_page_inspection_workflow_run_id = run_id
    copilot_ctx.composition_page_evidence = evidence
    if (
        isinstance(run_id, str)
        and run_id
        and getattr(copilot_ctx, "post_run_page_observation_after_failed_test", False)
    ):
        page_title = evidence.get("page_title")
        await _maybe_run_completion_verification_from_page_observation(
            copilot_ctx,
            url=str(evidence.get("current_url") or current_url or ""),
            title=page_title if isinstance(page_title, str) else "",
            observed_data=evidence,
        )
    earned_interaction = False
    if use_current_page and not run_id:
        earned_interaction = _consume_pending_browser_interaction_observation(
            copilot_ctx,
            current_url=str(evidence.get("current_url") or current_url or ""),
            evidence=evidence,
        )
    reached_via = _inspection_reached_via(
        use_current_page=use_current_page,
        post_run=bool(run_id),
        earned_interaction=earned_interaction,
    )
    observation_step = _append_flow_evidence(copilot_ctx, evidence, reached_via=reached_via)
    if observation_step is None:
        LOG.warning("copilot_flow_evidence_append_failed_no_trajectory")
    _mark_page_inspected(copilot_ctx)
    # Surface the reached page at the top level so the model registers that the
    # inspection already navigated there and does not re-issue navigate_browser.
    current_url = evidence.get("current_url") or evidence.get("inspected_url") or ""
    result = {
        "ok": True,
        "current_url": current_url,
        "reached_via": reached_via,
        "data": evidence,
    }
    if observation_step is not None:
        result["observation_step"] = observation_step
    record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
    return result


async def _discovery_walk(
    ctx: CopilotContext,
    *,
    entry_url: str,
    intent_hint: str,
) -> dict[str, Any]:
    """Deterministic anchor-scoring walker. No inner LLM call.

    Reads each visited page, looks for a strong title/H1 match with the
    intent_hint, surfaces a form if one is present, and otherwise follows
    the highest-scored same-origin anchor whose text matches the
    intent_hint. Bounded by step / wall-clock caps.
    """
    intent_tokens = set(_DISCOVERY_TOKEN_RE.findall(intent_hint.lower())) if intent_hint else set()
    evidence_trail: list[dict[str, Any]] = []
    current_url = entry_url
    current_page_loaded = False
    retried_deep_link_from_origin = False
    started = ctx.discovery_started_monotonic or time.monotonic()

    for step in range(_DISCOVERY_STEP_CAP):
        ctx.discovery_step_count = step + 1
        elapsed = time.monotonic() - started
        if elapsed > _DISCOVERY_WALL_CLOCK_SECONDS:
            return _discovery_build_result(
                candidate_url=None,
                candidate_form_fields=[],
                evidence_trail=evidence_trail,
                confidence=0.0,
                failure_reason="wall_clock_limit",
            )

        if current_page_loaded:
            current_page_loaded = False
        else:
            nav_result = await _discovery_navigate(ctx, current_url)
            if not nav_result.get("ok"):
                evidence_trail.append(
                    {
                        "url": current_url,
                        "page_title": "",
                        "transition_reason": f"navigate_failed: {nav_result.get('error', 'unknown')}"[:240],
                    }
                )
                # A pre-composition browser/session failure is not evidence that
                # the user omitted a page URL. Return the resolved entry URL so the
                # agent can test a minimal goto_url block and gather real run/debug
                # evidence before deciding whether to ask a follow-up.
                return _discovery_build_result(
                    candidate_url=current_url,
                    candidate_form_fields=[],
                    evidence_trail=evidence_trail,
                    confidence=_DISCOVERY_NAVIGATION_FALLBACK_CONFIDENCE,
                    failure_reason=None,
                )

            current_url = _discovery_extract_current_url(nav_result, current_url)
        # Survive the MCP size cap: a heavy DOM exceeds it and the html field is dropped, so
        # fall back to a stripped-body evaluate that keeps the links/forms the resolver needs
        # to identify a usable entrypoint. (Discovery only resolves the entrypoint, so a sliced
        # tail does not matter here.)
        html, _, _, _ = await _composition_get_html(ctx)
        page_title, anchors, form_fields = _discovery_parse_html(html)

        evidence_trail.append(
            {
                "url": current_url,
                "page_title": page_title[:240],
                "transition_reason": "initial" if step == 0 else "anchor_match",
            }
        )

        title_score = _discovery_title_score(page_title, intent_tokens)

        best_score = 0
        best_href: str | None = None
        best_anchor: dict[str, str] | None = None
        for anchor in anchors:
            score = _discovery_anchor_score(
                anchor.get("text", ""),
                anchor.get("title", ""),
                anchor.get("href", ""),
                intent_tokens,
            )
            if score > best_score:
                resolved = _discovery_resolve_href(current_url, anchor.get("href", ""))
                if resolved is None:
                    continue
                best_score = score
                best_href = resolved
                best_anchor = anchor

        evidence_tokens = _discovery_candidate_evidence_tokens(intent_tokens)
        candidate_title_score = _discovery_title_score(page_title, evidence_tokens)
        candidate_anchor_score = 0
        for anchor in anchors:
            candidate_anchor_score = max(
                candidate_anchor_score,
                _discovery_anchor_score(
                    anchor.get("text", ""),
                    anchor.get("title", ""),
                    anchor.get("href", ""),
                    evidence_tokens,
                ),
            )

        anti_bot_detected = _discovery_detect_anti_bot(html, page_title)
        anti_bot_has_no_candidate_evidence = (
            not form_fields and candidate_title_score == 0 and candidate_anchor_score == 0
        )
        if anti_bot_detected and anti_bot_has_no_candidate_evidence:
            origin_url = _discovery_origin_url(current_url)
            if (
                not retried_deep_link_from_origin
                and origin_url
                and origin_url != current_url
                and _discovery_should_retry_from_origin(entry_url, current_url)
            ):
                evidence_trail[-1]["transition_reason"] = "direct_deep_link_anti_bot"
                current_url = origin_url
                retried_deep_link_from_origin = True
                continue
            return _discovery_build_result(
                candidate_url=None,
                candidate_form_fields=[],
                evidence_trail=evidence_trail,
                confidence=0.0,
                failure_reason="anti_bot_wall",
            )
        if _discovery_detect_login_wall(html, page_title):
            return _discovery_build_result(
                candidate_url=None,
                candidate_form_fields=[],
                evidence_trail=evidence_trail,
                confidence=0.0,
                failure_reason="login_wall",
            )

        if intent_tokens and title_score >= 2 and (form_fields or best_score <= title_score):
            confidence = min(1.0, title_score / max(1, len(intent_tokens)))
            return _discovery_build_result(
                candidate_url=current_url,
                candidate_form_fields=form_fields,
                evidence_trail=evidence_trail,
                confidence=confidence,
                failure_reason=None,
            )

        if form_fields and (title_score >= 1 or step > 0):
            confidence = 0.6 if title_score >= 1 else 0.4
            return _discovery_build_result(
                candidate_url=current_url,
                candidate_form_fields=form_fields,
                evidence_trail=evidence_trail,
                confidence=confidence,
                failure_reason=None,
            )

        if not intent_tokens:
            return _discovery_build_result(
                candidate_url=current_url,
                candidate_form_fields=form_fields,
                evidence_trail=evidence_trail,
                confidence=0.3,
                failure_reason=None,
            )

        if best_score == 0 or best_href is None:
            return _discovery_build_result(
                candidate_url=None,
                candidate_form_fields=[],
                evidence_trail=evidence_trail,
                confidence=0.0,
                failure_reason="no_candidate",
            )

        if retried_deep_link_from_origin and best_anchor is not None:
            click_result = await _discovery_click_anchor(ctx, best_anchor)
            if click_result.get("ok"):
                current_url = _discovery_extract_current_url(click_result, best_href)
                # The next loop should inspect the clicked page instead of
                # navigating back to the original entry URL.
                current_page_loaded = True
                continue
            evidence_trail.append(
                {
                    "url": best_href,
                    "page_title": "",
                    "transition_reason": f"anchor_click_failed: {click_result.get('error', 'unknown')}"[:240],
                }
            )

        current_url = best_href

    return _discovery_build_result(
        candidate_url=None,
        candidate_form_fields=[],
        evidence_trail=evidence_trail,
        confidence=0.0,
        failure_reason="step_limit",
    )


async def _discover_workflow_entrypoint_impl(
    copilot_ctx: Any,
    site_or_url: str,
    intent_hint: str,
) -> dict[str, Any]:
    """Discovery tool body — separated from the @function_tool wrapper so
    tests can drive it with a stand-in ctx without the SDK's invocation
    machinery.
    """
    arguments = {"site_or_url": site_or_url, "intent_hint": intent_hint}

    def finish(result: dict[str, Any], *, site_or_url_kind: str | None = None) -> dict[str, Any]:
        _record_discovery_resolution_on_ctx(copilot_ctx, result)
        record_tool_step_result_for_ctx(copilot_ctx, "discover_workflow_entrypoint", arguments, result)
        data_payload = result.get("data")
        data = data_payload if isinstance(data_payload, Mapping) else {}
        candidate_url = data.get("candidate_url")
        failure_reason = data.get("failure_reason")
        if not isinstance(failure_reason, str) or not failure_reason:
            error = result.get("error")
            failure_reason = error if isinstance(error, str) and error else None
        LOG.info(
            "discover_workflow_entrypoint completed",
            ok=result.get("ok"),
            candidate_url=candidate_url if isinstance(candidate_url, str) and candidate_url else None,
            failure_reason=failure_reason,
            site_or_url_kind=site_or_url_kind,
        )
        return result

    authority_error = _authority_tool_error(copilot_ctx, "discover_workflow_entrypoint")
    if authority_error:
        result = {"ok": False, "error": authority_error}
        return finish(result)

    if copilot_ctx.discovery_calls_this_turn >= _DISCOVERY_PER_TURN_BUDGET:
        result = _discovery_build_result(
            candidate_url=None,
            candidate_form_fields=[],
            evidence_trail=list(copilot_ctx.discovery_evidence_trail),
            confidence=0.0,
            failure_reason="discovery_already_completed_this_turn",
        )
        return finish(result)

    cumulative = copilot_ctx.prior_discovery_calls_made + copilot_ctx.discovery_calls_this_turn
    if cumulative >= _DISCOVERY_PER_CHAT_BUDGET:
        result = _discovery_build_result(
            candidate_url=None,
            candidate_form_fields=[],
            evidence_trail=[],
            confidence=0.0,
            failure_reason="discovery_budget_exhausted_for_chat",
        )
        return finish(result)

    entry_url, kind = _resolve_discovery_entry_url(site_or_url)
    if entry_url is None:
        result = _discovery_build_result(
            candidate_url=None,
            candidate_form_fields=[],
            evidence_trail=[],
            confidence=0.0,
            failure_reason="could_not_resolve_site_name",
        )
        return finish(result, site_or_url_kind=kind)

    if copilot_ctx.build_phase == BuildPhase.INITIAL:
        try:
            advance_to_discovering(copilot_ctx)
        except ValueError as exc:
            # Race or unexpected prior advance — proceed without re-transitioning,
            # but surface the impossible state so it shows up in production logs.
            LOG.warning(
                "discover_workflow_entrypoint phase transition to discovering rejected",
                error=str(exc),
                build_phase=copilot_ctx.build_phase.value,
            )
    copilot_ctx.discovery_calls_this_turn += 1

    concrete_homepage_url = _concrete_homepage_entrypoint(entry_url, kind)
    if concrete_homepage_url is not None:
        evidence_trail = [
            {
                "url": concrete_homepage_url,
                "page_title": "",
                "transition_reason": "concrete_domain_homepage",
            }
        ]
        result = _discovery_build_result(
            candidate_url=concrete_homepage_url,
            candidate_form_fields=[],
            evidence_trail=evidence_trail,
            # Lower than a scraped-page match because the fast path skips page inspection.
            confidence=_DISCOVERY_CONCRETE_HOMEPAGE_CONFIDENCE,
            failure_reason=None,
        )
        copilot_ctx.discovery_evidence_trail = list(evidence_trail)
        try:
            advance_to_composing(copilot_ctx, reason="discovery_concrete_domain_homepage")
        except ValueError as exc:
            LOG.warning(
                "discover_workflow_entrypoint phase transition to composing rejected",
                error=str(exc),
                build_phase=copilot_ctx.build_phase.value,
            )
        return finish(result, site_or_url_kind=kind)

    with copilot_span(
        "discover_workflow_entrypoint",
        data={
            "site_or_url_kind": kind,
            "intent_hint_len": len(intent_hint or ""),
            "phase_entered": copilot_ctx.build_phase.value,
        },
    ):
        try:
            result = await _discovery_walk(
                copilot_ctx,
                entry_url=entry_url,
                intent_hint=intent_hint or "",
            )
        except Exception as exc:
            LOG.exception("discover_workflow_entrypoint walker raised")
            result = {
                "ok": False,
                "data": {
                    "candidate_url": None,
                    "candidate_form_fields": [],
                    "evidence_trail": [],
                    "confidence": 0.0,
                    "failure_reason": None,
                },
                "error": f"discover_workflow_entrypoint failed: {exc}",
            }

    data_payload = result.get("data") or {}
    data: dict[str, Any] = data_payload if isinstance(data_payload, dict) else {}
    copilot_ctx.discovery_evidence_trail = list(data.get("evidence_trail", []))
    if result.get("ok") and data.get("candidate_url"):
        try:
            advance_to_composing(copilot_ctx, reason="discovery_returned_candidate")
        except ValueError as exc:
            LOG.warning(
                "discover_workflow_entrypoint phase transition to composing rejected",
                error=str(exc),
                build_phase=copilot_ctx.build_phase.value,
            )

    return finish(result, site_or_url_kind=kind)


@function_tool(name_override="discover_workflow_entrypoint", strict_mode=False)
async def discover_workflow_entrypoint_tool(
    ctx: RunContextWrapper,
    site_or_url: str,
    intent_hint: str,
) -> str:
    """Find the page a new workflow should start at when the user named a site but not the page.

    Use this BEFORE writing blocks when the user named a website (with a URL,
    a bare domain, or a single brand word) but no specific page. Accepts:
    a URL with or without scheme (``example.com/login`` is fine), a bare
    domain (``example.com``), or a single brand word (resolved as
    ``https://www.<word>.com``). English phrases ("the X website") return
    ``failure_reason=could_not_resolve_site_name`` — ASK_QUESTION for a URL.

    Returns ``candidate_url`` plus a short ``evidence_trail`` and any
    ``candidate_form_fields``. Use ``candidate_url`` as the ``url`` value
    on a ``goto_url`` block. Do NOT paste the evidence into workflow YAML.

    Budget: one successful call per turn, three per chat, eight page hops,
    sixty seconds. On any ``failure_reason``, ASK_QUESTION for a URL — do not
    retry. Discovery navigates and reads pages; it will NOT type, click form
    buttons, run JavaScript, or submit forms.
    """
    result = await _discover_workflow_entrypoint_impl(ctx.context, site_or_url, intent_hint)
    return json.dumps(result)


@function_tool(name_override="inspect_page_for_composition", strict_mode=False)
async def inspect_page_for_composition_tool(
    ctx: RunContextWrapper,
    target_url: str,
) -> str:
    """Inspect a known page before composing form/search workflow blocks.

    Use this after the entrypoint URL is known and before authoring blocks that
    fill fields, submit searches, filter results, or expand result rows. It
    can also inspect the current browser page after a run by passing
    target_url="current_page"; use that after partial/budgeted runs so you do
    not replay a search that already advanced the page.

    Returns observed page evidence: current URL, title, navigation targets, form
    fields with labels and selectors, submit/search controls, result containers,
    compact visible text excerpts, anti-bot indicators, and bounded visual
    challenge evidence when DOM evidence shows challenge state. The returned
    `observation_step` is the side-channel id to pass in `block_observation_refs`
    when a newly authored block acts on this observed page. Do NOT paste the
    evidence into workflow YAML; use it to ground concise block prompts. If a
    block run changes pages, inspect the reached page before authoring downstream
    form/search/result blocks. If the
    evidence shows required fields or controls that the user did not supply
    enough information for, ASK_QUESTION with that observed missing input. If
    evidence is sufficient, compose and run workflow blocks from the observed fields.
    If challenge_state.gates_submit_controls is true, treat challenge resolution
    as a prerequisite for submit/search; do not click a submit control while the
    latest inspected evidence says it is disabled. If a later test still leaves
    that submit/search control disabled after a challenge-resolution attempt,
    report the observed anti-bot blocker rather than retrying the same flow.
    """
    result = await _inspect_page_for_composition_impl(ctx.context, target_url)
    return json.dumps(result)


NATIVE_TOOLS = [
    update_workflow_tool,
    list_credentials_tool,
    run_blocks_tool,
    get_run_results_tool,
    update_and_run_blocks_tool,
    discover_workflow_entrypoint_tool,
    inspect_page_for_composition_tool,
]

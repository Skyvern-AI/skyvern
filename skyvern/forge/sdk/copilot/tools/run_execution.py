from __future__ import annotations

import asyncio
import base64
import json
import time
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
    clear_blocker_signal_for_reason_codes,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisRepairContract,
    build_diagnosis_repair_contract,
)
from skyvern.forge.sdk.copilot.failure_tracking import (
    ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY,
    PER_TOOL_BUDGET_FAILURE_CATEGORY,
    compute_action_sequence_fingerprint,
    update_repeated_failure_state,
)
from skyvern.forge.sdk.copilot.narration import NarratorState
from skyvern.forge.sdk.copilot.narration import handler_available as narration_handler_available
from skyvern.forge.sdk.copilot.narration import narrator_poll_tick
from skyvern.forge.sdk.copilot.outcome_verification_trace import record_completion_verification, record_gate_decision
from skyvern.forge.sdk.copilot.output_utils import (
    _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
    build_run_blocks_response,
    iter_failure_reasons,
    truncate_output,
)
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    ensure_browser_session,
)
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.schemas.workflows import BlockType
from skyvern.webeye.navigation import is_skip_inner_retry_error
from skyvern.webeye.utils.page import SkyvernFrame

from ._shared import (
    _FAILED_BLOCK_STATUSES,
    RUN_BLOCKS_SAFETY_CEILING_SECONDS,
    _completed_run_block_labels,
    _failed_run_block_labels,
    _fallback_page_info,
    _unverified_current_workflow_labels,
    _valid_runtime_anchor_url,
    _workflow_verification_evidence,
)
from .blockers import (
    _active_block_run_budget_seconds,
    _active_run_terminal_evidence_detected,
    _active_run_terminal_evidence_signal,
    _analyze_run_blocks,
    _looks_like_anti_bot_blocker,
    _pending_reconciliation_requires_input_signal,
    _run_blocks_structured_blocker_message,
    _safe_read_workflow_run,
    _trusted_post_drain_status,
)
from .completion import (
    _emit_completion_verification_trace,
    _outcome_failure_warrants_repair,
    _outcome_unverified_reason,
)
from .composition_capture import (
    ActiveRunTerminalEvidenceSample,
    _active_run_terminal_evidence_result,
    _active_run_terminal_evidence_sample,
    _active_run_terminal_monitor_enabled,
)
from .credentials import (
    _credential_ids_validation_error,
    _extract_credential_ids_from_tool_value,
    _extract_credential_ids_from_workflow_definition,
)
from .frontier import (
    _MAX_INCREMENTAL_PAGE_FRONTIER_LABELS,
    _blocks_by_label,
    _workflow_with_runtime_block_goal_context,
    _workflow_with_runtime_frontier_anchor,
    _workflow_with_runtime_frontier_starter_url_seed,
)
from .guardrails import (
    _parameter_binding_invariant_error,
    _placeholder_for_parameter_type,
)
from .scouting import _mark_page_inspected

LOG = structlog.get_logger()

_ACTIVE_RUN_TERMINAL_MONITOR_INITIAL_DELAY_SECONDS = 30.0
_ACTIVE_RUN_TERMINAL_MONITOR_INTERVAL_SECONDS = 30.0
_ACTIVE_RUN_TERMINAL_MONITOR_MAX_SAMPLES = 8

# Primary exit condition: seconds of no observed progress across the combined
# run / block / step heartbeat. Sized to accommodate the slowest single LLM
# round-trip (~30-60 s in practice) with headroom; going tighter risks
# false-positives on healthy runs.
RUN_BLOCKS_STAGNATION_WINDOW_SECONDS = 90


# 5 s balances responsiveness (18 samples inside the stagnation window) against
# DB load (240 polls worst case at the safety ceiling).
RUN_BLOCKS_POLL_INTERVAL_SECONDS = 5.0

# Detached cleanup tasks held here so the garbage collector does not drop them
# while they still have work to do, and so the "task exception was never
# retrieved" warning cannot fire — each task adds a done-callback that logs
# exceptions and removes itself from this set.
_DETACHED_CLEANUP_TASKS: set[asyncio.Task] = set()


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
    if completion_verification is not None and completion_verification.status == "evaluated":
        copilot_ctx.last_outcome_gate_reason = _outcome_unverified_reason(copilot_ctx, completion_verification)
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
    record_gate_decision(
        copilot_ctx,
        {
            "run_output_blocker_detected": bool(structured_blocker),
            "run_output_empty_data_blocks": bool(empty_data_blocks),
        },
    )
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
                "produced no meaningful output "
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

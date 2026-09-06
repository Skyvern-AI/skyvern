from __future__ import annotations

import ast
import asyncio
import base64
import json
import os
import tempfile
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, NotRequired, TypedDict
from urllib.parse import urlparse

import structlog

from skyvern.exceptions import CopilotInlineSequentialCredentialUnsupported
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.copilot.active_run_session import (
    ActiveRunSessionAssociation,
    clear_active_run_session,
    publish_active_run_session,
)
from skyvern.forge.sdk.copilot.authoring_parameter_binding import _literal_locator_selector
from skyvern.forge.sdk.copilot.blocker_signal import (
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.budget_expiry import budget_run_denial
from skyvern.forge.sdk.copilot.build_test_connect_failure import (
    SUPERSEDED_BY_NEWER_TEST_REASON,
    BuildTestConnectFailure,
    BuildTestConnectFailureState,
    build_test_connect_failure_sentence,
)
from skyvern.forge.sdk.copilot.build_test_outcome import (
    INFRASTRUCTURE_RUNNER_ERROR_CODES,
    BuildTestEvidencePacket,
    BuildTestPacketDownload,
    BuildTestPacketFailure,
    BuildTestPacketLocatorObservation,
    BuildTestPacketLocatorUnobservedReason,
    BuildTestPacketPageCapture,
    BuildTestPacketPageState,
    BuildTestPacketRegisteredOutput,
    BuildTestPacketRequestedOutput,
    BuildTestPacketRun,
    BuildTestPacketRunBrowser,
    BuildTestPacketScreenshot,
    BuildTestPacketUnfinishedItem,
    RecordedBuildTestOutcome,
    authored_block_parameter_keys_from_workflow,
    authored_structure_signature_from_workflow,
    bind_post_run_page_evidence,
    connect_failure_from_run_blocks_result,
    failed_operation_from_run_blocks_result,
    post_run_page_capture_from_result,
    prior_attempt_change_identity,
    record_build_test_outcome,
    recorded_outcome_from_run_blocks_result,
    unresolved_runtime_block_failure_with_disposition,
)
from skyvern.forge.sdk.copilot.challenge_evidence import (
    ChallengeEvidenceSource,
    ChallengeKind,
    carrier_backed_anti_bot_categories,
    composition_challenge_carrier,
    first_carrier_backed_anti_bot_source,
)
from skyvern.forge.sdk.copilot.code_block_security import (
    COPILOT_CODE_SECURITY_FAILURE_CATEGORY,
    CodeBlockSecurityInput,
    runtime_code_security_errors,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    code_contains_credential_fill,
    trajectory_has_credential_fill,
)
from skyvern.forge.sdk.copilot.completion_output_grounding import page_evidence_prose_text
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    CriterionVerdict,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    resolved_locator_selector_candidates_expression,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    has_bounded_page_schema,
    model_visible_composition_evidence,
    parse_composition_html,
    stamp_page_evidence_provenance,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext, PageObstruction
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisRepairContract,
    build_diagnosis_repair_contract,
)
from skyvern.forge.sdk.copilot.failure_tracking import _blocks_by_label, block_shape_hashes_by_label
from skyvern.forge.sdk.copilot.frontier_provenance_dump import frontier_dump_root, trust_snapshot, write_packet
from skyvern.forge.sdk.copilot.narration import NarratorState
from skyvern.forge.sdk.copilot.narration import handler_available as narration_handler_available
from skyvern.forge.sdk.copilot.narration import narrator_poll_tick
from skyvern.forge.sdk.copilot.outcome_verification_trace import record_gate_decision
from skyvern.forge.sdk.copilot.output_utils import (
    _INTERNAL_GOAL_PATH_OMISSIONS_KEY,
    _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
    _INTERNAL_RUN_OUTCOME_RECORDED_KEY,
    BUILD_TEST_PACKET_KEY,
    build_run_blocks_response,
    iter_failure_reasons,
    project_build_test_packet_for_llm,
    sanitize_tool_result_for_llm,
)
from skyvern.forge.sdk.copilot.review_gate import workflow_block_fingerprints
from skyvern.forge.sdk.copilot.run_outcome import (
    TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE,
    RecordedRunOutcome,
    RunOutcomeReasonCode,
    RunOutcomeRole,
    RunOutcomeVerdict,
    recorded_output_report,
    run_outcome_display_reason,
    trusted_terminal_challenge_category_name,
)
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    FrontierStartProvenance,
    OriginRunRedactionRegistry,
    PreRunPageReference,
    RegisteredArtifactEntry,
    RegisteredArtifactEvidence,
    browser_page_custody_lock,
    ensure_build_test_browser_session,
    record_sensitive_origin_run_taint,
    register_sensitive_origin_run_lease,
    release_sensitive_origin_run_lease,
    resolve_persistent_browser_state,
    verify_build_test_browser_session_by_attaching,
)
from skyvern.forge.sdk.copilot.runtime_authoring_repair import (
    build_test_page_state_from_evidence,
    inject_runtime_authoring_repair_context,
    post_run_inspection_cleanly_matches,
    record_pending_runtime_authoring_repair_context,
    repair_page_evidence_is_admissible,
    same_run_typed_challenge_kind,
)
from skyvern.forge.sdk.copilot.screenshot_utils import (
    ScreenshotActionRelation,
    ScreenshotProvenance,
    enqueue_screenshot,
)
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.secret_scrub import (
    register_matching_origin_run_redaction_values,
    register_secret_scrub_values_from_structure,
    scrub_secrets_from_structure,
)
from skyvern.forge.sdk.copilot.terminal_envelope import interim_run_start_outcome, is_interim_run_outcome
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.turn_halt import (
    stash_build_test_superseded_halt,
    stash_turn_halt_from_blocker_signal,
)
from skyvern.forge.sdk.copilot.workflow_yaml import _process_workflow_yaml, runner_code_block_associations
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.schemas.credentials import CredentialVaultType
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotRunOutcomeUpdate,
    WorkflowCopilotRunStartedUpdate,
    WorkflowCopilotStreamMessageType,
)
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.utils.pdf_parser import extract_pdf_file
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.code_block_recorder import RECORDED_FAILURE_RESPONSE_MAX_CHARS
from skyvern.forge.sdk.workflow.models.parameter import (
    OutputParameter,
    WorkflowParameter,
    WorkflowParameterType,
    is_sensitive_workflow_parameter,
)
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun, WorkflowRunStatus
from skyvern.forge.sdk.workflow.runtime_completion import contract_from_request_criteria
from skyvern.forge.sdk.workflow.runtime_secret_bridge import consume_copilot_runtime_secret_values
from skyvern.forge.sdk.workflow.service import run_selection_is_partial
from skyvern.schemas.workflows import BlockStatus, BlockType
from skyvern.utils.files import initialize_skyvern_state_file
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ActionStatus
from skyvern.webeye.navigation import is_skip_inner_retry_error
from skyvern.webeye.utils.page import SkyvernFrame

from ._shared import (
    _FAILED_BLOCK_STATUSES,
    RUN_BLOCKS_SAFETY_CEILING_SECONDS,
    _completed_run_block_labels,
    _composition_unverified_current_workflow_labels,
    _current_workflow_block_labels,
    _failed_run_block_labels,
    _fallback_page_info,
    _unverified_current_workflow_labels,
    _valid_runtime_anchor_url,
    _workflow_definition_block_labels,
    _workflow_verification_evidence,
)
from .banned_blocks import _copilot_block_authoring_policy
from .blockers import (
    _analyze_run_blocks,
    _artifact_challenge_flag_from_result,
    _looks_like_anti_bot_blocker,
    _run_blocks_structured_blocker_message,
    _safe_read_workflow_run,
    _trusted_post_drain_status,
)
from .completion import _artifact_health_blocker_from_result
from .composition_capture import (
    _read_run_session_page_evidence,
    store_post_run_page_evidence,
)
from .credentials import (
    _approve_server_verified_google_sheet_bindings,
    _credential_ids_validation_error,
    _credential_run_approval_blocker_signal,
    _credential_run_approval_error,
    _extract_credential_ids_for_labels,
    _extract_credential_ids_from_tool_value,
    _extract_credential_ids_from_workflow_definition,
    _google_connection_reference_ids,
    _google_sheet_connection_bindings_from_workflow_definition,
    _server_verified_google_account_choices,
)
from .frontier import (
    _workflow_with_runtime_block_goal_context,
    _workflow_with_runtime_frontier_anchor,
    _workflow_with_runtime_frontier_starter_url_seed,
)
from .guardrails import (
    _authority_tool_error,
    _parameter_binding_invariant_error,
    _placeholder_for_parameter_type,
)
from .scouting import _mark_post_run_page_observed, _redact_codeblock_value

LOG = structlog.get_logger()


_MAX_REGISTERED_ARTIFACTS = 3
_MAX_REGISTERED_ARTIFACT_BYTES = 5 * 1024 * 1024
_MAX_REGISTERED_ARTIFACT_TEXT_CHARS = 20_000
_REGISTERED_ARTIFACT_PARSE_EXTENSIONS = frozenset({".txt", ".csv", ".json"})

_POST_RUN_PAGE_HTML_ARTIFACT_TYPES = (ArtifactType.HTML_ACTION, ArtifactType.HTML_SCRAPE)
_MAX_POST_RUN_PAGE_HTML_CHARS = 1_500_000
_POST_RUN_PAGE_PARSE_TIMEOUT_SECONDS = 15.0
# Non-S3 backends ignore an artifact's bundle_key and return the whole ZIP; page HTML never
# starts with a ZIP local/central/spanning signature, so treat these prefixes as fail-closed.
_ZIP_MAGIC_PREFIXES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# Primary exit condition: seconds of no observed progress across the combined
# run / block / step heartbeat. Sized to accommodate the slowest single LLM
# round-trip (~30-60 s in practice) with headroom; going tighter risks
# false-positives on healthy runs.
RUN_BLOCKS_STAGNATION_WINDOW_SECONDS = 90


# 5 s balances responsiveness (18 samples inside the stagnation window) against
# DB load (240 polls worst case at the safety ceiling).
RUN_BLOCKS_POLL_INTERVAL_SECONDS = 5.0

COPILOT_SANDBOX_UNAVAILABLE_ERROR = "Sandboxed worker is unavailable; execution was not started."

# Block types that can reach exec() in the API process, so a run containing one may
# only proceed on the sandboxed worker. CODE compiles user code directly;
# WORKFLOW_TRIGGER runs its child in-process with block_labels=None, which both
# executes the child's own code blocks and re-opens the cached-script import path;
# TaskV2 synthesizes a code block at runtime from planner output. The latter two
# cannot be proven code-free by inspecting the draft, so they stay fail-closed.
_SANDBOX_REQUIRED_BLOCK_TYPES = frozenset(
    {
        BlockType.CODE.value,
        BlockType.WORKFLOW_TRIGGER.value,
        BlockType.TaskV2.value,
    }
)

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
    except (TimeoutError, asyncio.CancelledError):
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


async def _cooperative_cancel_dispatched_run(workflow_run_id: str) -> None:
    """Best-effort cooperative cancel of a worker-dispatched copilot run.

    The run executes on the worker (Temporal), so there is no in-process ``run_task`` to
    cancel/drain. We flip the DB status to ``canceled`` (a no-op when already terminal) so the
    worker stops at the next step boundary. Unlike the inline path's 5s task drain this is
    cooperative-first; a true Temporal ``workflow_handle.cancel()`` is a follow-up.
    """
    try:
        await app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final(
            workflow_run_id=workflow_run_id,
        )
    except Exception:
        LOG.warning(
            "Cooperative cancel of dispatched copilot run failed",
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )


async def _delete_dispatch_draft(workflow_id: str, organization_id: str) -> None:
    """Best-effort soft-delete of the copilot dispatch version once the run is done.

    Test versions are excluded from saved-workflow resolution while still addressable by exact
    ID. Terminal cleanup retires the version after its executor has finished; a cleanup failure
    does not expose the proposal as the saved version and must never fail the run.
    """
    try:
        await app.DATABASE.workflows.soft_delete_workflow_by_id(
            workflow_id=workflow_id,
            organization_id=organization_id,
        )
    except Exception:
        LOG.warning(
            "Failed to soft-delete copilot dispatch draft",
            workflow_id=workflow_id,
            organization_id=organization_id,
            exc_info=True,
        )


async def _delete_dispatch_draft_if_run_final(workflow_id: str, workflow_run_id: str, organization_id: str) -> None:
    """Soft-delete the dispatch draft, but only once the run is in a final state.

    The worker resolves the pinned draft via get_workflow(run.workflow_id) when it picks the run
    up. Deleting the draft while the run is still non-final — e.g. an unexpected exception bubbles
    out of the poll loop before the worker loads it — would make that resolution 404 with
    WorkflowNotFound. A non-final exit leaves the draft in place rather than racing the worker.
    """
    run = await app.DATABASE.workflow_runs.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )
    if run is None or not run.status.is_final():
        LOG.info(
            "Skipping copilot dispatch draft delete; run is not final yet",
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            status=run.status if run else None,
        )
        return
    await _delete_dispatch_draft(workflow_id, organization_id)


async def _retire_snapshot_after_execution(
    run_task: asyncio.Task,
    workflow_id: str,
    workflow_run_id: str,
    organization_id: str,
) -> None:
    try:
        await asyncio.shield(run_task)
    finally:
        await _delete_dispatch_draft_if_run_final(workflow_id, workflow_run_id, organization_id)


def _log_detached_cleanup_failure(task: asyncio.Task) -> None:
    exc = task.exception() if task.done() and not task.cancelled() else None
    if exc is not None:
        LOG.warning("Detached cancel fallback failed", exc_info=exc)


def _copilot_sandbox_unavailable_result(*, organization_id: str, workflow_permanent_id: str) -> dict[str, Any]:
    # No repair can make the sandbox reachable, so the result carries
    # UNRECOVERABLE_TOOL_ERROR to reach the contract's STOP lane. Without it the
    # refusal classifies as a generic FAILED_RUN and the enforcement loop nudges
    # the model to retry a run that never started.
    # The single choke point every "sandbox unavailable" return goes through, so the
    # org/workflow refusal rate is measurable regardless of which caller refused.
    LOG.info(
        "Copilot block run refused: sandboxed worker unavailable",
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
    )
    return {
        "ok": False,
        "error": COPILOT_SANDBOX_UNAVAILABLE_ERROR,
        "data": {
            "workflow_run_id": None,
            "overall_status": "failed",
            "failure_reason": COPILOT_SANDBOX_UNAVAILABLE_ERROR,
            "blocks": [],
            "failure_categories": [
                {
                    "category": "UNRECOVERABLE_TOOL_ERROR",
                    "confidence_float": 1.0,
                    "reasoning": "Sandboxed execution was unavailable; no workflow run was created.",
                }
            ],
        },
    }


async def _attach_action_traces(
    blocks: list[WorkflowRunBlock],
    results: list[dict[str, Any]],
    organization_id: str,
    *,
    include_completed: bool = False,
) -> None:
    """Fetch compact retained actions for failed blocks, or every block in the just-finished run."""
    task_ids = [
        block.task_id
        for block, result in zip(blocks, results)
        if block.task_id and (include_completed or result.get("status") in _FAILED_BLOCK_STATUSES)
    ]
    if not task_ids:
        return

    try:
        rows = await app.DATABASE.tasks.get_recent_actions_for_tasks(
            task_ids=task_ids,
            organization_id=organization_id,
        )
    except Exception:
        if not include_completed:
            raise
        LOG.warning(
            "Failed to load optional completed-run action observations",
            organization_id=organization_id,
            task_count=len(task_ids),
            exc_info=True,
        )
        return

    actions_by_task: dict[str, list[Action]] = defaultdict(list)
    for row in rows:
        if row.task_id is not None:
            actions_by_task[row.task_id].append(row)

    for block, block_result in zip(blocks, results):
        task_id = block.task_id
        if not task_id or (not include_completed and block_result.get("status") not in _FAILED_BLOCK_STATUSES):
            continue
        task_actions = actions_by_task.get(task_id, [])
        newest_step_id = next(
            (step_id for action in task_actions if isinstance(step_id := action.step_id, str) and step_id),
            None,
        )
        if newest_step_id is not None:
            block_result["step_id"] = newest_step_id
        action_trace = []
        for action in task_actions:
            entry: dict[str, str | int | None] = {
                "action": action.action_type,
                "status": action.status,
                "reasoning": action.reasoning[:150] if action.reasoning else None,
                "element": action.element_id,
            }
            output = action.output
            code_line = output.get("code_line") if isinstance(output, dict) else None
            if action.status == ActionStatus.failed and type(code_line) is int:
                # code_line is the code-block recorder's stamp. Gating on it keeps this to the
                # recorder's own rows: personalize_action writes the typed-in field value to
                # response, which is user data and must not reach the packet.
                entry["code_line"] = code_line
                if action.response:
                    # Persistence masks registered secrets and parameters; a token the block
                    # obtained at runtime is in neither registry, so screen it here too.
                    entry["response"] = redact_raw_secrets_for_prompt(action.response)[
                        :RECORDED_FAILURE_RESPONSE_MAX_CHARS
                    ]
            action_trace.append(entry)
        block_result["action_trace"] = action_trace


def _recorded_run_block_result(block: WorkflowRunBlock) -> dict[str, Any]:
    """Project one persisted run-block row without dropping its machine identity."""
    result: dict[str, Any] = {
        "label": block.label,
        "block_type": block.block_type.name,
        "status": block.status,
    }
    result["workflow_run_block_id"] = block.workflow_run_block_id
    if block.task_id:
        result["task_id"] = block.task_id
    if block.failure_reason:
        result["failure_reason"] = block.failure_reason
    if block.error_codes:
        result["error_codes"] = list(block.error_codes)
    # The persisted row owns null too. Keeping the key distinguishes an explicit null from an
    # older result that carried no block-output fact at all.
    result["output"] = block.output
    return result


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


async def _fetch_run_block_screenshot_b64(workflow_run_block_id: str, organization_id: str) -> str | None:
    try:
        artifact = await app.DATABASE.artifacts.get_artifact_by_entity_id(
            artifact_type=ArtifactType.SCREENSHOT_LLM,
            organization_id=organization_id,
            workflow_run_block_id=workflow_run_block_id,
        )
        if not artifact:
            return None
        artifact_bytes = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
        if not artifact_bytes:
            return None
        return base64.b64encode(artifact_bytes).decode("utf-8")
    except Exception:
        LOG.debug(
            "Failed to fetch run-block screenshot for failed block",
            workflow_run_block_id=workflow_run_block_id,
            exc_info=True,
        )
        return None


async def _fetch_failed_block_screenshot_b64(block: Any, organization_id: str) -> str | None:
    """Resolve a failed block's at-failure screenshot.

    Code blocks persist theirs on the workflow_run_block, so read that first. The task_v2
    lookup filters on observer_cruise_id — it can never match a code block — and is kept only
    so runs whose screenshots resolved through it before keep resolving.
    """
    if block.workflow_run_block_id:
        b64 = await _fetch_run_block_screenshot_b64(block.workflow_run_block_id, organization_id)
        if b64 is not None:
            return b64
    if block.task_id:
        return await _fetch_last_screenshot_b64(block.task_id, organization_id)
    return None


async def _attach_failed_block_screenshots(
    blocks: list,
    results: list[dict[str, Any]],
    organization_id: str,
) -> None:
    """Attach the at-failure screenshot and final URL to every failed block that has them."""
    failed = [
        (block, block_result)
        for block, block_result in zip(blocks, results)
        if block_result.get("status") in _FAILED_BLOCK_STATUSES
    ]
    if not failed:
        return

    screenshots = await asyncio.gather(
        *(_fetch_failed_block_screenshot_b64(block, organization_id) for block, _ in failed),
    )
    for (block, block_result), b64 in zip(failed, screenshots):
        if b64 is not None:
            block_result["screenshot_b64"] = b64
        if block.final_url:
            block_result["final_url"] = block.final_url
        if b64 is None and not block.final_url:
            block_result["at_failure_evidence"] = "No at-failure screenshot or final URL was persisted for this block."
        if b64 is not None or block.final_url:
            LOG.info(
                "Attached at-failure evidence to failed block",
                workflow_run_block_id=block.workflow_run_block_id,
                block_type=block_result.get("block_type"),
                has_screenshot=b64 is not None,
                final_url=block.final_url,
            )


def _resolve_run_screenshot_b64(
    *,
    live_capture: str | None,
    results: list[dict[str, Any]],
    run_ok: bool,
) -> str | None:
    """Pick the screenshot the model sees for this run.

    Only data.screenshot_base64 becomes a model-visible image, and the live capture that fills
    it is skipped on dispatched runs — so a failed run falls back to its first failed block's
    at-failure screenshot. A successful run never promotes one: a healed or continue_on_failure
    block must not put a failure image in front of the model.
    """
    if live_capture is not None:
        return live_capture
    if run_ok:
        return None
    return next((r["screenshot_b64"] for r in results if r.get("screenshot_b64")), None)


async def _recorded_watchdog_block_receipts(workflow_run_id: str, organization_id: str) -> list[dict[str, Any]]:
    try:
        blocks = await app.DATABASE.observer.get_workflow_run_blocks(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
    except Exception:
        LOG.debug("Failed to load block receipts after watchdog exit", workflow_run_id=workflow_run_id, exc_info=True)
        return []
    # The same projection a normal run's receipts get: a block that failed before the watchdog exit
    # failed for a reason, and dropping it here left that failure with no identity to repair.
    return [_recorded_run_block_result(block) for block in blocks if block.status]


def _forget_browser_position(ctx: CopilotContext) -> None:
    """Drop the claim about where the browser stopped, keeping the verified prefix itself."""
    ctx.verified_prefix_block_end_urls = {}
    ctx.verified_prefix_block_end_session_id = None
    ctx.verified_prefix_terminal_label = None


def _forget_interim_run_start(ctx: CopilotContext, workflow_run_id: str) -> None:
    """Drop the run-start record when submission unwound, so no terminal reports a run that never ran."""
    outcomes = ctx.terminal_envelope_run_outcomes
    outcomes[:] = [
        outcome
        for outcome in outcomes
        if not (is_interim_run_outcome(outcome) and outcome.workflow_run_id == workflow_run_id)
    ]


def _block_end_urls_by_label(run_block_rows: list[WorkflowRunBlock]) -> dict[str, str]:
    """Page each labelled block ended on, chronologically, from worker-persisted run rows."""
    end_urls: dict[str, str] = {}
    for block in run_block_rows:
        final_url = _valid_runtime_anchor_url(block.final_url)
        if block.label and final_url is not None:
            end_urls[block.label] = final_url
    return end_urls


async def _resolve_post_run_page_info(
    ctx: CopilotContext,
    *,
    run_block_rows: list[WorkflowRunBlock],
    dispatch_to_worker: bool,
    sensitive_origin_run: bool,
    run_session_id: str | None,
) -> tuple[str, str, str | None]:
    """Return model-visible terminal page facts without reading or retaining a sensitive origin."""
    if sensitive_origin_run:
        return "", "", None
    if dispatch_to_worker:
        dispatched_end_url = _dispatched_end_url(run_block_rows)
        return dispatched_end_url or "", "", dispatched_end_url
    current_url, page_title = await _fallback_page_info(ctx, session_id_override=run_session_id)
    return current_url, page_title, None


NO_PERSISTED_END_URL = "No worker-persisted final URL exists for this run; the page it ended on is unknown."

# block.py masks the at-failure URL before persisting it, so a login/MFA failure can leave a
# final_url that parses but is not the page: it cannot be resumed and must not be reported as one.
_SECRET_MASK = "*****"
DISPATCHED_END_URL_MAX_CHARS = 2000


def _dispatched_end_url(run_block_rows: list[WorkflowRunBlock]) -> str | None:
    """Page the run ended on, read from the terminal worker-persisted row because the session is worker-owned.

    Only the terminal row counts: an earlier block's URL is a different page, and reporting it would
    answer "where did the run end" with a page the run left.
    """
    if not run_block_rows:
        return None
    final_url = _valid_runtime_anchor_url(run_block_rows[-1].final_url)
    if final_url is None or _SECRET_MASK in final_url:
        return None
    # The mask only covers the secret and parameter registries; a token the block picked up at
    # runtime is in neither, and a URL is as good a carrier for one as an error message is.
    screened = redact_raw_secrets_for_prompt(final_url)
    # Refuse rather than truncate: a cut URL still parses, so it would be reported as the page the
    # run ended on while being unresumable -- the same thing the mask check above returns None for.
    if len(screened) > DISPATCHED_END_URL_MAX_CHARS:
        return None
    return screened


def _failing_code_line(action_trace: Any) -> int | None:
    """The code-block recorder's line stamp on the newest failed action, if it recorded one."""
    if not isinstance(action_trace, list):
        return None
    for entry in action_trace:
        if not isinstance(entry, dict):
            continue
        code_line = entry.get("code_line")
        if type(code_line) is int:
            return code_line
    return None


def _summarize_action_trace(action_trace: list[dict[str, Any]] | None) -> list[str]:
    """Render the six newest action entries chronologically for the compact packet."""
    if not action_trace:
        return []
    summary: list[str] = []
    for entry in reversed(action_trace[:6]):
        if not isinstance(entry, dict):
            continue
        action = entry.get("action") or "?"
        status = entry.get("status") or ""
        element = entry.get("element")
        response = entry.get("response")
        code_line = entry.get("code_line")
        bits = [str(action)]
        if element:
            # A selector the block built from runtime page text is in no secret registry, the same
            # residual the response screen below exists for.
            bits.append(redact_raw_secrets_for_prompt(str(element)))
        if status:
            bits.append(str(status))
        if isinstance(response, str) and response:
            bits.append(f"response={response}")
        if type(code_line) is int:
            bits.append(f"code_line={code_line}")
        summary.append(" ".join(bits).strip())
    return summary


def _retained_action_observations(results: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render the newest six safe action facts chronologically from newest-block-first results."""
    bounded_newest: list[dict[str, Any]] = []
    entries_seen = 0
    for block_result in results:
        action_trace = block_result.get("action_trace")
        if not isinstance(action_trace, list):
            continue
        for entry in action_trace:
            entries_seen += 1
            if isinstance(entry, dict):
                bounded_newest.append(entry)
            if entries_seen == 6:
                break
        if entries_seen == 6:
            break

    observations: list[str] = []
    for entry in reversed(bounded_newest):
        raw_action = entry.get("action")
        raw_status = entry.get("status")
        if not isinstance(raw_action, str) or not isinstance(raw_status, str):
            continue
        try:
            action = ActionType(raw_action)
            status = ActionStatus(raw_status)
        except ValueError:
            continue
        observation = f"{action.value} {status.value}"
        code_line = entry.get("code_line")
        if type(code_line) is int:
            observation += f" code_line={code_line}"
        observations.append(observation)
    return observations


def _failure_action_trace_summary(failed_result: Mapping[str, Any] | None) -> list[str]:
    """Keep code-recorder diagnostics; native task rows use only the safe typed projection."""
    if failed_result is None:
        return []
    action_trace = failed_result.get("action_trace")
    if str(failed_result.get("block_type") or "").upper() == BlockType.CODE.name and isinstance(action_trace, list):
        return _summarize_action_trace(action_trace)
    return _retained_action_observations([failed_result])


# Watchdog exit reasons. ``success`` means the run reached a trustworthy
# terminal status inside the poll loop OR after the post-drain reconcile.
# The run-ending reasons share the reconcile path but produce distinct
# error messages: ``stagnation`` is the primary trip (no progress signals
# for ``RUN_BLOCKS_STAGNATION_WINDOW_SECONDS`` seconds), ``ceiling`` is the
# last-resort budget-exhausted branch, and ``task_exit_unfinalized`` is the
# rare race where ``execute_workflow`` naturally exits before writing a
# terminal row. ``paused`` is the exception: the run is alive and waiting on a
# person, so it is neither cancelled nor reconciled.
WatchdogExitReason = Literal[
    "success",
    "stagnation",
    "ceiling",
    "task_exit_unfinalized",
    "paused",
]


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
        BlockType.FILE_UPLOAD.value,
        # A code block writes its row on entry and exit and nothing in between, so a long
        # login or wait inside one is indistinguishable from a frozen run.
        BlockType.CODE.value,
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
    dispatch_to_worker: bool = False,
) -> str:
    """LLM-facing error string for a non-success watchdog exit. No variant uses
    "timed out" or other retry-inviting phrasing — those are SKY-9163 traps.

    ``dispatch_to_worker`` runs skip the ``_fallback_page_info`` CDP read: the worker
    owns the run's persistent browser session, so the API must not attach to it.
    """
    suppress_live_page = dispatch_to_worker or _origin_registry_contains_sensitive_values(
        ctx.origin_run_redaction_registry,
        workflow_run_id,
    )
    if exit_reason == "stagnation":
        body = (
            f"The run has not made progress for {RUN_BLOCKS_STAGNATION_WINDOW_SECONDS}s. "
            f"No step, block, or workflow-run row updates were observed in that window. "
            f"The page is most likely blocked by a captcha, popup, anti-bot challenge, "
            f"hidden validation error, or an infinite-retry loop on an action the agent "
            f"cannot detect is failing."
        )
    elif exit_reason == "paused":
        # A pause is a healthy waiting state rather than an uncertain outcome, so it returns here
        # instead of picking up the shared "outcome is uncertain, do not re-invoke" tail below. This
        # is the one arm that directs the model to relay its own text, so it carries no run id.
        return (
            "The run is paused at a human_interaction block, waiting for a person to approve or "
            "reject it. It stays paused until someone acts on it in Skyvern or the block's timeout "
            "elapses; nothing was cancelled. Tell the user the run is paused and what it is waiting "
            "for, and do not re-run these blocks."
        )
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
    current_url, _ = ("", "") if suppress_live_page else await _fallback_page_info(ctx)
    if current_url:
        message += f" Browser was on: {current_url}"
    return message


def _watchdog_user_facing_summary(
    exit_reason: WatchdogExitReason,
    budget_seconds: int,
    run: WorkflowRun | None,
) -> str:
    if exit_reason == "stagnation":
        return f"The run stopped after no observable progress for {RUN_BLOCKS_STAGNATION_WINDOW_SECONDS}s."
    if exit_reason == "paused":
        return "The run is paused, waiting for a person to approve or reject it."
    if exit_reason == "ceiling":
        return f"The run exceeded the {budget_seconds}s absolute ceiling while still showing progress."
    if run is not None:
        return f"The run ended before recording a trustworthy terminal status. Last observed status: {run.status}."
    return "The run ended before recording a trustworthy terminal status."


def _workflow_covers_labels(workflow: Workflow | None, labels: list[str]) -> bool:
    return workflow is not None and all(workflow.get_output_parameter(label) for label in labels)


def _workflow_has_blocks(workflow: Workflow | None) -> bool:
    if workflow is None:
        return False
    definition = workflow.workflow_definition
    if isinstance(definition, dict):
        return bool(definition.get("blocks"))
    return bool(definition.blocks)


async def _workflow_from_prior_draft(ctx: CopilotContext, labels: list[str]) -> Workflow | None:
    """Returns None on empty/malformed yaml or when it still misses a label, so the
    caller falls through to the existing not-found error."""
    workflow_yaml = ctx.prior_copilot_workflow_yaml
    if not workflow_yaml or not workflow_yaml.strip():
        return None
    try:
        workflow = await _process_workflow_yaml(
            workflow_id=ctx.workflow_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
            workflow_yaml=workflow_yaml,
        )
    except Exception:
        # Prior-parse is best-effort; a settings-inherit lookup failure must not block the run tool.
        LOG.warning("Could not parse prior copilot draft for run-tool label resolution", exc_info=True)
        return None
    return workflow if _workflow_covers_labels(workflow, labels) else None


def _should_use_fresh_session_for_login_first_replay(
    ctx: AgentContext,
    labels_to_execute: list[str],
    workflow: Workflow | None,
) -> bool:
    """Fresh session when this run replays a login fill into the scout's authenticated session.

    Keyed on two planes the agent cannot edit between runs — the scout trajectory authenticated
    via a credential fill and any executed block in this run fills one; frontier re-runs seeded
    past login carry no credential fill and keep reusing the scout session.
    """
    if not trajectory_has_credential_fill(ctx.scout_trajectory):
        return False
    return _labels_replay_login_fill(labels_to_execute, workflow)


def _labels_replay_login_fill(labels_to_execute: list[str], workflow: Workflow | None) -> bool:
    if not labels_to_execute or workflow is None:
        return False
    code_inputs = _selected_code_security_inputs(
        _workflow_definition_blocks_for_code_security(workflow.workflow_definition),
        selected_labels=set(labels_to_execute),
    )
    return any(code_contains_credential_fill(code_input.code) for code_input in code_inputs)


def _runtime_code_security_failure_for_selected_labels(
    workflow: Workflow,
    *,
    block_labels: list[str],
    labels_to_execute: list[str],
    frontier_start_label: str | None,
) -> dict[str, Any] | None:
    code_blocks = _selected_code_security_inputs(
        _workflow_definition_blocks_for_code_security(workflow.workflow_definition),
        selected_labels=set(labels_to_execute),
    )
    errors = runtime_code_security_errors(code_blocks)
    if not errors:
        return None

    failure_reason = "Copilot runtime blocked unsafe synthesized code before browser dispatch."
    return {
        "ok": False,
        "error": failure_reason,
        "data": {
            "workflow_run_id": None,
            "overall_status": "failed",
            "failure_reason": failure_reason,
            "requested_block_labels": list(block_labels),
            "executed_block_labels": [],
            "planned_block_labels": list(labels_to_execute),
            "frontier_start_label": frontier_start_label,
            "blocks": [],
            "failure_categories": [error.to_failure_category() for error in errors],
            "failure_category": COPILOT_CODE_SECURITY_FAILURE_CATEGORY,
        },
    }


_INLINE_SEQUENTIAL_CREDENTIAL_USER_REASON = (
    "This test run uses a credential that is set to be used by one run at a time, and the copilot's "
    "in-process test run cannot hold that ordering. The run stopped instead of using the credential "
    "alongside another run."
)


def _inline_sequential_credential_fence_failure(
    *,
    workflow_run_id: str,
    sequential_credential_id: str | None,
    dispatch_to_worker: bool,
    block_labels: list[str],
    labels_to_execute: list[str],
    frontier_start_label: str | None,
) -> dict[str, Any] | None:
    # The copilot inline path runs execute_workflow in-process: the run never queues, never gets a
    # queued_at, and never reaches the Temporal V2 serialization gate, yet setup stamped its
    # sequential_credential_id. The gate filters queued_at IS NOT NULL, so a concurrent run sharing the
    # credential would neither see this run nor be waited on by it. Fail closed before it uses the
    # credential — the same fence the scheduled and sync-trigger paths apply. The dispatch path enqueues
    # through the executor (stamped queued_at, gated), so it is exempt.
    if dispatch_to_worker or not sequential_credential_id:
        return None
    return {
        "ok": False,
        "error": str(CopilotInlineSequentialCredentialUnsupported(workflow_run_id)),
        "data": {
            "workflow_run_id": workflow_run_id,
            "overall_status": "failed",
            "failure_reason": _INLINE_SEQUENTIAL_CREDENTIAL_USER_REASON,
            "user_facing_summary": _INLINE_SEQUENTIAL_CREDENTIAL_USER_REASON,
            "requested_block_labels": list(block_labels),
            "executed_block_labels": [],
            "planned_block_labels": list(labels_to_execute),
            "frontier_start_label": frontier_start_label,
            "blocks": [],
        },
    }


def _workflow_definition_blocks_for_code_security(workflow_definition: Any) -> list[Any]:
    if isinstance(workflow_definition, Mapping):
        blocks = workflow_definition.get("blocks")
    else:
        blocks = getattr(workflow_definition, "blocks", None)
    return list(blocks) if isinstance(blocks, list) else []


def _selected_code_security_inputs(
    blocks: list[Any],
    *,
    selected_labels: set[str],
    include_descendants: bool = False,
) -> list[CodeBlockSecurityInput]:
    code_blocks: list[CodeBlockSecurityInput] = []
    for block in blocks:
        if isinstance(block, Mapping):
            label = str(block.get("label") or "")
            selected = include_descendants or label in selected_labels
            block_type = str(block.get("block_type") or "").lower()
            code = block.get("code")
            if block_type == BlockType.CODE.value and selected and isinstance(code, str):
                code_blocks.append(CodeBlockSecurityInput(label=label, code=code))
            code_blocks.extend(
                _selected_code_security_inputs(
                    _mapping_child_blocks(block),
                    selected_labels=selected_labels,
                    include_descendants=selected,
                )
            )
            continue
        label = str(getattr(block, "label", "") or "")
        selected = include_descendants or label in selected_labels
        if isinstance(block, CodeBlock) and selected:
            code_blocks.append(CodeBlockSecurityInput(label=block.label, code=block.code))
        if children := _typed_child_blocks(block):
            code_blocks.extend(
                _selected_code_security_inputs(
                    children,
                    selected_labels=selected_labels,
                    include_descendants=selected,
                )
            )
    return code_blocks


def _requested_completion_contract(
    ctx: CopilotContext,
    runtime_workflow: Workflow,
    labels_to_execute: list[str],
) -> dict[str, object] | None:
    """The turn's own deliverable obligation, for a test run that executes the whole workflow.

    The obligation attaches to the workflow only when a proposal is accepted, which is after the
    test run this grades; a selection that runs only part of the workflow stays ungraded."""
    if ctx.request_policy is None:
        return None
    if not runtime_workflow.workflow_definition.blocks:
        return None
    if run_selection_is_partial(runtime_workflow, labels_to_execute):
        return None
    return contract_from_request_criteria(ctx.request_policy.graded_completion_criteria())


def _selected_blocks_require_sandbox(
    blocks: list[Any],
    *,
    selected_labels: set[str],
    include_descendants: bool = False,
) -> bool:
    for block in blocks:
        if isinstance(block, Mapping):
            label = str(block.get("label") or "")
            block_type = str(block.get("block_type") or "").lower()
            children = _mapping_child_blocks(block)
        else:
            label = str(getattr(block, "label", "") or "")
            block_type = str(getattr(block, "block_type", "") or "").lower()
            children = _typed_child_blocks(block)
        selected = include_descendants or label in selected_labels
        if selected and block_type in _SANDBOX_REQUIRED_BLOCK_TYPES:
            return True
        if children and _selected_blocks_require_sandbox(
            children,
            selected_labels=selected_labels,
            include_descendants=selected,
        ):
            return True
    return False


def _mapping_child_blocks(block: Mapping[str, Any]) -> list[Any]:
    children: list[Any] = []
    for key in ("loop_blocks", "blocks"):
        value = block.get(key)
        if isinstance(value, list):
            children.extend(value)
    for key in ("branch_conditions", "branches", "ordered_branches"):
        branches = block.get(key)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            if isinstance(branch, Mapping):
                children.extend(_mapping_child_blocks(branch))
    return children


def _typed_child_blocks(block: Any) -> list[Any]:
    children: list[Any] = []
    for key in ("loop_blocks", "blocks"):
        value = getattr(block, key, None)
        if isinstance(value, list):
            children.extend(value)
    for key in ("branch_conditions", "branches", "ordered_branches"):
        branches = getattr(block, key, None)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            if isinstance(branch, Mapping):
                children.extend(_mapping_child_blocks(branch))
            else:
                children.extend(_typed_child_blocks(branch))
    return children


def _workflow_output_parameter_indexes(
    workflow: Workflow | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if workflow is None:
        return {}, {}
    workflow_definition = getattr(workflow, "workflow_definition", None)
    blocks = (
        workflow_definition.get("blocks")
        if isinstance(workflow_definition, Mapping)
        else getattr(workflow_definition, "blocks", None)
    )
    if not isinstance(blocks, list):
        return {}, {}

    by_id: dict[str, dict[str, Any]] = {}
    by_key: dict[str, dict[str, Any]] = {}

    def visit(block: Any) -> None:
        output_parameter = (
            block.get("output_parameter") if isinstance(block, Mapping) else getattr(block, "output_parameter", None)
        )
        output_parameter_id = (
            output_parameter.get("output_parameter_id")
            if isinstance(output_parameter, Mapping)
            else getattr(output_parameter, "output_parameter_id", None)
        )
        output_parameter_key = (
            output_parameter.get("key")
            if isinstance(output_parameter, Mapping)
            else getattr(output_parameter, "key", None)
        )
        label = block.get("label") if isinstance(block, Mapping) else getattr(block, "label", None)
        block_type = block.get("block_type") if isinstance(block, Mapping) else getattr(block, "block_type", None)
        block_type_name = getattr(block_type, "value", getattr(block_type, "name", block_type))
        entry = {
            "block_label": label if isinstance(label, str) and label else None,
            "block_type": str(block_type_name) if block_type_name is not None else None,
            "output_parameter_id": output_parameter_id if isinstance(output_parameter_id, str) else None,
            "output_parameter_key": output_parameter_key if isinstance(output_parameter_key, str) else None,
        }
        if entry["output_parameter_id"]:
            by_id[entry["output_parameter_id"]] = entry
        if entry["output_parameter_key"]:
            by_key[entry["output_parameter_key"]] = entry
        for child in _mapping_child_blocks(block) if isinstance(block, Mapping) else _typed_child_blocks(block):
            visit(child)

    for block in blocks:
        visit(block)
    return by_id, by_key


def _workflow_parameters(workflow: Workflow | None) -> list[Any]:
    if workflow is None:
        return []
    workflow_definition = workflow.workflow_definition
    parameters = (
        workflow_definition.get("parameters")
        if isinstance(workflow_definition, Mapping)
        else getattr(workflow_definition, "parameters", None)
    )
    return parameters if isinstance(parameters, list) else []


ExecutionSnapshotProvenance = Literal["canonical", "staged"]


@dataclass(frozen=True)
class CopilotExecutionSnapshot:
    """One provenance-closed workflow definition and the rows a run binds against."""

    provenance: ExecutionSnapshotProvenance
    workflow: Workflow
    workflow_parameters: tuple[WorkflowParameter, ...]
    output_parameters: tuple[OutputParameter, ...]


def _declared_execution_snapshot(workflow: Workflow) -> CopilotExecutionSnapshot:
    workflow = workflow.model_copy(deep=True)
    parameters = _workflow_parameters(workflow)
    return CopilotExecutionSnapshot(
        provenance="staged",
        workflow=workflow,
        workflow_parameters=tuple(parameter for parameter in parameters if isinstance(parameter, WorkflowParameter)),
        output_parameters=tuple(parameter for parameter in parameters if isinstance(parameter, OutputParameter)),
    )


async def _select_execution_snapshot(
    ctx: CopilotContext,
    requested_labels: Sequence[str],
) -> CopilotExecutionSnapshot | None:
    """Select staged bytes or one exact committed version; never combine their declarations."""
    if ctx.staged_workflow is not None:
        return _declared_execution_snapshot(ctx.staged_workflow)

    workflow = await app.DATABASE.workflows.get_workflow(
        workflow_id=ctx.workflow_id,
        organization_id=ctx.organization_id,
    )
    if not _workflow_has_blocks(workflow):
        prior_draft = await _workflow_from_prior_draft(ctx, list(requested_labels))
        if prior_draft is not None:
            return _declared_execution_snapshot(prior_draft)
    if workflow is None:
        return None

    workflow_parameters, output_parameters = await asyncio.gather(
        app.WORKFLOW_SERVICE.get_workflow_parameters(workflow_id=workflow.workflow_id),
        app.DATABASE.workflow_params.get_workflow_output_parameters(workflow_id=workflow.workflow_id),
    )
    return CopilotExecutionSnapshot(
        provenance="canonical",
        workflow=workflow,
        workflow_parameters=tuple(workflow_parameters),
        output_parameters=tuple(output_parameters),
    )


def _materialized_execution_snapshot(source: CopilotExecutionSnapshot, workflow: Workflow) -> CopilotExecutionSnapshot:
    """Replace a staged source with the immutable version returned by normal persistence."""
    parameters = _workflow_parameters(workflow)
    return CopilotExecutionSnapshot(
        provenance=source.provenance,
        workflow=workflow,
        workflow_parameters=tuple(parameter for parameter in parameters if isinstance(parameter, WorkflowParameter)),
        output_parameters=tuple(parameter for parameter in parameters if isinstance(parameter, OutputParameter)),
    )


def _merge_registered_output_parameter_values_into_blocks(data: dict[str, Any]) -> None:
    """Mutate ``data["blocks"]`` so registered output parameters share the block-output evidence path."""

    registered = data.get("registered_output_parameter_values")
    if not isinstance(registered, list) or not registered:
        return
    blocks = data.get("blocks")
    if not isinstance(blocks, list):
        blocks = []
        data["blocks"] = blocks
    by_label: dict[str, dict[str, Any]] = {
        block["label"]: block
        for block in blocks
        if isinstance(block, dict) and isinstance(block.get("label"), str) and block.get("label")
    }
    for item in registered:
        if not isinstance(item, Mapping):
            continue
        label = item.get("block_label")
        key = item.get("output_parameter_key")
        if not isinstance(label, str) or not label or not isinstance(key, str) or not key:
            continue
        value = item.get("value")
        block = by_label.get(label)
        if block is None:
            block = {
                "label": label,
                "block_type": item.get("block_type") or "CODE",
            }
            blocks.append(block)
            by_label[label] = block
        extracted = block.get("extracted_data")
        if isinstance(extracted, dict):
            extracted.setdefault(key, value)
        elif extracted is None:
            block["extracted_data"] = {key: value}


async def _attach_registered_output_parameter_values(
    *,
    workflow_run_id: str,
    workflow: Workflow | None,
    data: dict[str, Any],
    persisted_output_parameters: list[Any] | None = None,
) -> dict[str, Any]:
    try:
        registered_rows = await app.DATABASE.workflow_runs.get_workflow_run_output_parameters(
            workflow_run_id=workflow_run_id
        )
    except Exception:
        LOG.warning(
            "Failed to read workflow run output parameters for copilot run evidence; "
            "deterministic graders lose authoritative output-parameter evidence",
            workflow_run_id=workflow_run_id,
            organization_id=getattr(workflow, "organization_id", None),
            exc_info=True,
        )
        data["registered_output_values_omission"] = "persisted registered output values were unavailable"
        return {}
    if not registered_rows:
        data["registered_output_parameter_values"] = []
        return {}

    index_by_id, index_by_key = _workflow_output_parameter_indexes(workflow)
    persisted_key_by_id = {
        output_parameter_id: key
        for parameter in persisted_output_parameters or []
        if isinstance((output_parameter_id := getattr(parameter, "output_parameter_id", None)), str)
        and isinstance((key := getattr(parameter, "key", None)), str)
    }
    normalized: list[dict[str, Any]] = []
    values_by_label: dict[str, Any] = {}
    for row in registered_rows:
        output_parameter_id = getattr(row, "output_parameter_id", None)
        if not isinstance(output_parameter_id, str) or not output_parameter_id:
            continue
        block_info = dict(index_by_id.get(output_parameter_id, {}))
        output_parameter_key = block_info.get("output_parameter_key")
        if not isinstance(output_parameter_key, str) or not output_parameter_key:
            output_parameter_key = persisted_key_by_id.get(output_parameter_id)
            if isinstance(output_parameter_key, str):
                block_info["output_parameter_key"] = output_parameter_key
        if output_parameter_key and not block_info.get("block_label"):
            block_info.update(index_by_key.get(output_parameter_key, {}))
        value = getattr(row, "value", None)
        item = {
            "workflow_run_id": workflow_run_id,
            "output_parameter_id": output_parameter_id,
            "output_parameter_key": block_info.get("output_parameter_key"),
            "block_label": block_info.get("block_label"),
            "block_type": block_info.get("block_type"),
            "value": value,
        }
        normalized.append(item)
        label = item.get("block_label")
        key = item.get("output_parameter_key")
        if isinstance(label, str) and label and isinstance(key, str) and key:
            values_by_label.setdefault(label, {})[key] = value

    data["registered_output_parameter_values"] = normalized
    _merge_registered_output_parameter_values_into_blocks(data)
    return values_by_label


def _requested_output_parameter_definitions(*, workflow_run_id: str, workflow: Workflow) -> list[dict[str, str | None]]:
    index_by_id, _ = _workflow_output_parameter_indexes(workflow)
    definitions: list[dict[str, str | None]] = []
    for parameter in _workflow_parameters(workflow):
        if not isinstance(parameter, OutputParameter):
            continue
        block_info = index_by_id.get(parameter.output_parameter_id, {})
        definitions.append(
            {
                "workflow_run_id": workflow_run_id,
                "output_parameter_id": parameter.output_parameter_id,
                "output_parameter_key": parameter.key,
                "description": parameter.description,
                "block_label": block_info.get("block_label"),
                "block_type": block_info.get("block_type"),
            }
        )
    return definitions


def _pin_pre_run_page_reference(ctx: CopilotContext, run_id: str) -> None:
    evidence = ctx.composition_page_evidence
    if not isinstance(evidence, Mapping):
        return
    text = page_evidence_prose_text(evidence).strip()
    if not text:
        return
    ctx.pre_run_page_reference = PreRunPageReference(text=text, workflow_run_id=run_id)


def _runs_this_turn(copilot_ctx: AgentContext) -> int:
    count = copilot_ctx.block_run_calls_this_turn
    return count if isinstance(count, int) else 0


def _recorded_fresh_run_session_fact(result: Mapping[str, Any]) -> bool | None:
    data = result.get("data")
    fact = data.get("used_fresh_run_session") if isinstance(data, dict) else None
    return fact if isinstance(fact, bool) else None


def _terminal_challenge_kind(copilot_ctx: AgentContext, result: Mapping[str, Any]) -> ChallengeKind | None:
    data = result.get("data")
    run_id = data.get("workflow_run_id") if isinstance(data, dict) else None
    return same_run_typed_challenge_kind(copilot_ctx.composition_page_evidence, run_id)


def _attach_run_session_facts(
    data: dict[str, Any],
    *,
    used_fresh_run_session: bool,
    run_detached_from_chat: bool,
    run_ok: bool,
    page_evidence: Mapping[str, Any] | None,
) -> None:
    """Stamp the run-session facts onto a result envelope built after the fresh-session
    decision. ``challenge_stalled_fresh_session`` is omitted rather than set False when no
    structured page packet exists, so absent reads as unknown."""
    data["used_fresh_run_session"] = used_fresh_run_session
    # A carried resume browser is also not the chat's, so detachment is the honest fact for
    # "can I look at this run's page from here", not whether the session was minted.
    data["run_detached_from_chat"] = run_detached_from_chat
    if not isinstance(page_evidence, Mapping):
        return
    data["challenge_stalled_fresh_session"] = (
        used_fresh_run_session and not run_ok and composition_challenge_carrier(page_evidence) is not None
    )


def _same_run_page_evidence_for_result(ctx: CopilotContext, run_id: str) -> dict[str, Any] | None:
    evidence = ctx.composition_page_evidence
    if not isinstance(evidence, dict):
        return None
    if not post_run_inspection_cleanly_matches(evidence, run_id):
        return None
    redacted = _redact_codeblock_value(ctx, evidence)
    return dict(redacted) if isinstance(redacted, dict) else None


def _artifact_file_name(artifact: Artifact) -> str:
    uri = artifact.uri if isinstance(artifact.uri, str) else ""
    return uri.rsplit("/", 1)[-1] if uri else artifact.artifact_id


def _parse_registered_artifact_text(file_name: str, artifact_bytes: bytes) -> str | None:
    suffix = os.path.splitext(file_name)[1].lower()
    if suffix == ".pdf":
        temp_file_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
                temp_file_path = temp_file.name
                temp_file.write(artifact_bytes)
            return extract_pdf_file(temp_file_path, file_identifier=file_name) or None
        except Exception:
            return None
        finally:
            if temp_file_path is not None:
                try:
                    os.unlink(temp_file_path)
                except OSError:
                    pass
    if suffix in _REGISTERED_ARTIFACT_PARSE_EXTENSIONS:
        try:
            return artifact_bytes.decode("utf-8", errors="ignore") or None
        except Exception:
            return None
    return None


def _collect_downloaded_artifact_ids(block_outputs_by_label: Mapping[str, Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for output in block_outputs_by_label.values():
        if not isinstance(output, dict):
            continue
        raw = output.get("downloaded_file_artifact_ids")
        if not isinstance(raw, list):
            continue
        for artifact_id in raw:
            if isinstance(artifact_id, str) and artifact_id and artifact_id not in seen:
                seen.add(artifact_id)
                ordered.append(artifact_id)
    return ordered


async def _fetch_registered_download_artifacts(
    *, run_id: str, organization_id: str, downloaded_artifact_ids: Sequence[str] | None
) -> list[Artifact]:
    # The run's own download artifact ids are same-run by construction, so keying off them
    # avoids depending on the DOWNLOAD row's workflow_run_id stamp across repair-iteration run ids.
    if downloaded_artifact_ids:
        artifacts = await app.DATABASE.artifacts.get_artifacts_by_ids(
            list(dict.fromkeys(downloaded_artifact_ids)),
            organization_id=organization_id,
        )
        by_id = {
            artifact.artifact_id: artifact for artifact in artifacts if artifact.artifact_type == ArtifactType.DOWNLOAD
        }
        return [by_id[artifact_id] for artifact_id in dict.fromkeys(downloaded_artifact_ids) if artifact_id in by_id]
    result = await app.DATABASE.artifacts.get_artifacts_for_run(
        run_id,
        organization_id=organization_id,
        artifact_types=[ArtifactType.DOWNLOAD],
    )
    return result if isinstance(result, list) else []


async def _capture_registered_artifact_evidence(
    ctx: CopilotContext,
    *,
    run_id: str,
    organization_id: str,
    downloaded_artifact_ids: Sequence[str] | None = None,
) -> None:
    try:
        artifacts = await _fetch_registered_download_artifacts(
            run_id=run_id,
            organization_id=organization_id,
            downloaded_artifact_ids=downloaded_artifact_ids,
        )
    except Exception:
        LOG.debug("Registered-artifact evidence fetch failed", run_id=run_id, exc_info=True)
        return
    entries: list[RegisteredArtifactEntry] = []
    total_chars = 0
    for artifact in artifacts[:_MAX_REGISTERED_ARTIFACTS]:
        file_name = _artifact_file_name(artifact)
        suffix = os.path.splitext(file_name)[1].lower()
        if suffix != ".pdf" and suffix not in _REGISTERED_ARTIFACT_PARSE_EXTENSIONS:
            continue
        file_size = artifact.file_size
        if isinstance(file_size, int) and file_size > _MAX_REGISTERED_ARTIFACT_BYTES:
            LOG.debug("Skipping oversize registered artifact", artifact_id=artifact.artifact_id, file_size=file_size)
            continue
        try:
            artifact_bytes = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
        except Exception:
            LOG.debug("Registered-artifact retrieve failed", artifact_id=artifact.artifact_id, exc_info=True)
            continue
        if not artifact_bytes or len(artifact_bytes) > _MAX_REGISTERED_ARTIFACT_BYTES:
            continue
        parsed_text = _parse_registered_artifact_text(file_name, artifact_bytes)
        if not parsed_text:
            continue
        remaining = _MAX_REGISTERED_ARTIFACT_TEXT_CHARS - total_chars
        if remaining <= 0:
            break
        clipped = parsed_text[:remaining]
        total_chars += len(clipped)
        entries.append(
            RegisteredArtifactEntry(artifact_id=artifact.artifact_id, file_name=file_name, parsed_text=clipped)
        )
    if entries:
        ctx.registered_artifact_evidence = RegisteredArtifactEvidence(entries=tuple(entries), workflow_run_id=run_id)


# The failure this enrichment describes is often a wedged page, so its own reads need a
# ceiling: page.evaluate has no action timeout, and hanging here would cancel the whole tool
# and lose the run failure that was already recorded.
_OBSERVED_LOCATOR_BUDGET_SECONDS = 8.0


class AuthoredLocatorObservationRow(TypedDict):
    authored_selector: str
    match_count: NotRequired[int]
    match_index: NotRequired[Literal[0]]
    observed_candidates: NotRequired[list[str]]
    unobserved_reason: NotRequired[BuildTestPacketLocatorUnobservedReason]


def _unobserved_locator_row(
    selector: str,
    reason: BuildTestPacketLocatorUnobservedReason,
) -> AuthoredLocatorObservationRow:
    return {"authored_selector": selector, "unobserved_reason": reason}


def _first_failed_result(results: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return next((result for result in results if result.get("status") in _FAILED_BLOCK_STATUSES), None)


def _failed_block_code(workflow: Workflow, failed_result: Mapping[str, Any] | None) -> str | None:
    """The selected failed row's authored code, read from the definition rather than the run row.

    ``run_blocks`` works with workflow_run_block rows, which carry status and output but no code.
    A missing label cannot be correlated to definition code, so it must not fall through to a
    later failed row whose locators belong to a different failure.
    """
    if failed_result is None:
        return None
    failed_label = failed_result.get("label")
    if not isinstance(failed_label, str) or not failed_label:
        return None
    code_inputs = _selected_code_security_inputs(
        _workflow_definition_blocks_for_code_security(workflow.workflow_definition),
        selected_labels={failed_label},
    )
    return next((code_input.code for code_input in code_inputs if code_input.code), None)


def _authored_literal_locator_selectors(code: str) -> list[str]:
    """Every literal ``page.locator("...")`` the block names, ordered by source position.

    ``ast.walk`` is breadth-first, so a nested locator would otherwise surface after a later
    shallow one and the observations would not read in the order the block does.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    found: dict[str, tuple[int, int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        selector = _literal_locator_selector(node)
        if selector and selector not in found:
            found[selector] = (node.lineno, node.col_offset)
    return sorted(found, key=lambda selector: found[selector])


async def _observe_authored_locators(
    ctx: AgentContext,
    *,
    run_session_id: str | None,
    failed_block_code: str | None,
    worker_owned: bool = False,
    observation_deadline_exceeded: bool = False,
) -> list[AuthoredLocatorObservationRow] | None:
    """Resolve the failed block's own literal locators against the page the run ended on.

    Reports the count and the identities the page carried. It does not rank them or pick a
    replacement; the repair remains the model's.
    """
    if not failed_block_code:
        return None
    selectors = _authored_literal_locator_selectors(failed_block_code)
    if not selectors:
        return []
    if worker_owned:
        return [_unobserved_locator_row(selector, "worker_owned_run") for selector in selectors]
    if observation_deadline_exceeded:
        return [_unobserved_locator_row(selector, "observation_deadline_exceeded") for selector in selectors]
    if run_session_id is None:
        return [_unobserved_locator_row(selector, "run_browser_unavailable") for selector in selectors]

    collected: dict[str, AuthoredLocatorObservationRow] = {}

    async def _observe() -> BuildTestPacketLocatorUnobservedReason | None:
        try:
            browser_state = await resolve_persistent_browser_state(
                session_id=run_session_id,
                organization_id=ctx.organization_id,
            )
        except Exception:
            LOG.debug("Run browser was unavailable for authored-locator observation", exc_info=True)
            return "run_browser_unavailable"
        if browser_state is None:
            return "run_browser_unavailable"
        # Creating one would count every authored selector against a blank page, so a run whose
        # page has since closed would report as a dead locator rather than an unobserved one.
        try:
            page = await browser_state.get_working_page()
        except Exception:
            LOG.debug("Run page was unavailable for authored-locator observation", exc_info=True)
            return "run_page_unavailable"
        if page is None:
            return "run_page_unavailable"
        for selector in selectors:
            try:
                # Playwright's own engines (text=, :has-text, >> nth=) are exactly the locators most
                # likely to be the failure, and querySelectorAll cannot parse them.
                locator = page.locator(selector)
                match_count = await locator.count()
            except Exception:  # noqa: BLE001 - optional locator evidence must not replace the run failure
                # This is optional enrichment after the run failure was recorded. Preserve that
                # failure across browser/transport errors; cancellation and control flow inherit
                # from BaseException and still propagate.
                collected[selector] = _unobserved_locator_row(selector, "locator_resolution_failed")
                continue
            if not isinstance(match_count, int) or isinstance(match_count, bool) or match_count < 0:
                collected[selector] = _unobserved_locator_row(selector, "locator_resolution_failed")
                continue
            if match_count == 0:
                collected[selector] = {"authored_selector": selector, "match_count": 0}
                continue
            try:
                raw_candidates = await locator.nth(0).evaluate(
                    resolved_locator_selector_candidates_expression(selector)
                )
            except Exception:  # noqa: BLE001 - optional identity evidence must not replace the run failure
                # Identity collection is the same optional seam as resolution above: an ordinary
                # read failure is typed absence, while cancellation still escapes this handler.
                collected[selector] = _unobserved_locator_row(selector, "identity_read_failed")
                continue
            candidates: list[str] = []
            for candidate in raw_candidates if isinstance(raw_candidates, list) else []:
                if not isinstance(candidate, Mapping):
                    continue
                candidate_selector = candidate.get("selector")
                if isinstance(candidate_selector, str) and candidate_selector and candidate_selector not in candidates:
                    candidates.append(candidate_selector)
            if not candidates:
                collected[selector] = _unobserved_locator_row(selector, "identity_read_failed")
                continue
            collected[selector] = {
                "authored_selector": selector,
                "match_count": match_count,
                "match_index": 0,
                "observed_candidates": candidates,
            }
        return None

    incomplete_reason: BuildTestPacketLocatorUnobservedReason | None = None
    try:
        incomplete_reason = await asyncio.wait_for(_observe(), timeout=_OBSERVED_LOCATOR_BUDGET_SECONDS)
    except TimeoutError:
        # Partial results are kept: a timeout or a wedged browser is itself a fact about the page,
        # and discarding what was already read would report the attempt as never made.
        LOG.debug("Observing authored locators after the run did not finish", exc_info=True)
        incomplete_reason = "observation_deadline_exceeded"
    return [
        collected.get(selector) or _unobserved_locator_row(selector, incomplete_reason or "locator_resolution_failed")
        for selector in selectors
    ]


async def _capture_and_store_post_run_page(
    ctx: CopilotContext,
    *,
    run_session_id: str,
    run_id: str,
    current_url: str,
) -> BuildTestPacketPageCapture:
    """A failed or hollow capture neutralizes stale evidence to None only when it would not cleanly match
    this run_id, so the matcher's destructive clear cannot fire on the pending failure-string context."""
    sensitive_origin_run = register_matching_origin_run_redaction_values(ctx, run_id)
    registry = ctx.origin_run_redaction_registry
    if (
        registry is not None
        and registry.workflow_run_id == run_id
        and registry.contains_sensitive_values
        and not sensitive_origin_run
    ):
        ctx.composition_page_evidence = None
        return BuildTestPacketPageCapture(status="unavailable", omission="page_capture_unavailable")
    evidence, observed_session_id, _, captured_frame = await _read_run_session_page_evidence(
        ctx, run_session_id=run_session_id, current_url=current_url
    )
    if evidence is not None and repair_page_evidence_is_admissible(evidence):
        if sensitive_origin_run:
            evidence = scrub_secrets_from_structure(ctx, evidence)
            captured_frame = None
        _, preserved_stored_evidence = store_post_run_page_evidence(
            ctx,
            evidence,
            run_id=run_id,
            current_url=current_url,
            source_browser_session_id=observed_session_id,
            run_browser_session_id=run_session_id,
        )
        same_run_stored_evidence = _same_run_page_evidence_for_result(ctx, run_id)
        if (
            captured_frame is not None
            and not preserved_stored_evidence
            and same_run_stored_evidence is not None
            and not (
                ctx.origin_run_redaction_registry is not None
                and ctx.origin_run_redaction_registry.workflow_run_id == run_id
                and ctx.origin_run_redaction_registry.contains_sensitive_values
            )
        ):
            enqueue_screenshot(
                ctx,
                captured_frame.b64,
                provenance=ScreenshotProvenance(
                    source_tool="post_run_page_capture",
                    captured_url=captured_frame.captured_url,
                    observation_step=None,
                    browser_session_id=captured_frame.browser_session_id,
                    workflow_run_id=run_id,
                    action_relation=ScreenshotActionRelation.WORKFLOW_RUN_RESULT,
                    dispatch_url=captured_frame.dispatch_url,
                    dispatch_browser_session_id=captured_frame.dispatch_browser_session_id,
                    producer_browser_session_id=captured_frame.producer_browser_session_id,
                    session_binding=captured_frame.session_binding,
                ),
                captured_at=captured_frame.captured_at,
            )
        stored_capture = post_run_page_capture_from_result(
            {"workflow_run_id": run_id, "browser_session_id": run_session_id},
            same_run_stored_evidence,
        )
        return stored_capture or BuildTestPacketPageCapture(status="unavailable", omission="page_capture_unavailable")
    if not post_run_inspection_cleanly_matches(ctx.composition_page_evidence, run_id):
        ctx.composition_page_evidence = None
    return BuildTestPacketPageCapture(status="unavailable", omission="page_capture_unavailable")


def _pre_run_baseline_is_provenance_valid(evidence: Mapping[str, Any] | None) -> bool:
    """Pre-dispatch scout evidence carries no run stamp; post-run evidence from a prior turn does.
    Pinning the latter as this run's baseline would let a stale page launder a confirm, so reject it."""
    if not isinstance(evidence, Mapping):
        return False
    if evidence.get("observed_after_workflow_run") is True:
        return False
    return not evidence.get("workflow_run_id")


def _select_terminal_page_artifact(artifacts: Sequence[Artifact]) -> Artifact | None:
    for artifact_type in _POST_RUN_PAGE_HTML_ARTIFACT_TYPES:
        family = [artifact for artifact in artifacts if artifact.artifact_type == artifact_type]
        if family:
            return max(family, key=lambda artifact: (artifact.created_at, artifact.artifact_id))
    return None


def _workflow_requires_terminal_artifact_redaction(workflow: Workflow | None) -> bool:
    if workflow is None:
        return True
    return any(is_sensitive_workflow_parameter(parameter) for parameter in _workflow_parameters(workflow))


def _origin_registry_contains_sensitive_values(
    registry: OriginRunRedactionRegistry | None,
    workflow_run_id: str,
) -> bool:
    return registry is not None and registry.workflow_run_id == workflow_run_id and registry.contains_sensitive_values


def _sensitive_parameter_keys_requiring_resolved_values(workflow: Workflow) -> list[str]:
    return [
        parameter.key
        for parameter in _workflow_parameters(workflow)
        if is_sensitive_workflow_parameter(parameter)
        and not (
            isinstance(parameter, WorkflowParameter)
            and parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID
        )
    ]


def _mutable_redaction_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _mutable_redaction_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_redaction_value(item) for item in value]
    if isinstance(value, frozenset):
        return [_mutable_redaction_value(item) for item in value]
    return value


async def _fetch_dispatched_terminal_page_evidence(
    *,
    run_id: str,
    organization_id: str,
    current_url: str,
    workflow: Workflow | None,
    origin_redaction_registry: OriginRunRedactionRegistry | None,
) -> dict[str, Any] | None:
    # Persisted HTML is untrusted. A sensitive workflow requires the active producer's serialized
    # parameter registry, structurally bound to this exact run, before artifact bytes are read.
    matching_registry = (
        origin_redaction_registry
        if origin_redaction_registry is not None and origin_redaction_registry.workflow_run_id == run_id
        else None
    )
    origin_redaction_parameters = matching_registry.parameters if matching_registry is not None else None
    artifact_redaction_parameters = None
    if matching_registry is not None:
        artifact_redaction_parameters = matching_registry.artifact_parameters or matching_registry.parameters
    if workflow is None or (
        _workflow_requires_terminal_artifact_redaction(workflow)
        and (
            not origin_redaction_parameters
            or matching_registry is None
            or not matching_registry.contains_all_sensitive_values
        )
    ):
        return None
    try:
        result = await app.DATABASE.artifacts.get_artifacts_for_run(
            run_id,
            organization_id=organization_id,
            artifact_types=list(_POST_RUN_PAGE_HTML_ARTIFACT_TYPES),
        )
    except Exception:
        LOG.debug("Dispatched terminal page artifact fetch failed", run_id=run_id, exc_info=True)
        return None
    artifacts = result if isinstance(result, list) else []
    latest = _select_terminal_page_artifact(artifacts)
    if latest is None:
        return None
    file_size = latest.file_size
    if isinstance(file_size, int) and file_size > _MAX_REGISTERED_ARTIFACT_BYTES:
        return None
    try:
        artifact_bytes = await app.ARTIFACT_MANAGER.retrieve_artifact(latest)
    except Exception:
        LOG.debug("Dispatched terminal page retrieve failed", artifact_id=latest.artifact_id, exc_info=True)
        return None
    if not artifact_bytes or len(artifact_bytes) > _MAX_REGISTERED_ARTIFACT_BYTES:
        return None
    if artifact_bytes.startswith(_ZIP_MAGIC_PREFIXES):
        return None
    raw_html = artifact_bytes.decode("utf-8", errors="ignore")[:_MAX_POST_RUN_PAGE_HTML_CHARS]
    scrubbed_html = (
        app.AGENT_FUNCTION.redact_codeblock_parameter_values(
            raw_html, _mutable_redaction_value(artifact_redaction_parameters)
        )
        if artifact_redaction_parameters
        else raw_html
    )
    if not isinstance(scrubbed_html, str) or not scrubbed_html:
        return None
    try:
        evidence = await asyncio.wait_for(
            asyncio.to_thread(
                parse_composition_html,
                scrubbed_html,
                inspected_url=current_url,
                current_url=current_url,
            ),
            timeout=_POST_RUN_PAGE_PARSE_TIMEOUT_SECONDS,
        )
    except Exception:
        LOG.debug("Dispatched terminal page parse failed", run_id=run_id, exc_info=True)
        return None
    return evidence if isinstance(evidence, dict) else None


async def _capture_dispatched_terminal_page_evidence(
    ctx: CopilotContext,
    *,
    workflow: Workflow | None = None,
    run_id: str,
    run_session_id: str,
    organization_id: str,
    current_url: str,
    origin_redaction_registry: OriginRunRedactionRegistry | None = None,
) -> None:
    """Read the run's own browser session first; the worker HTML artifact is only a fallback because
    its presence never proved the run had reached its terminal page."""
    if _pre_run_baseline_is_provenance_valid(ctx.composition_page_evidence):
        _pin_pre_run_page_reference(ctx, run_id)
    matching_sensitive_registry = _origin_registry_contains_sensitive_values(origin_redaction_registry, run_id)
    source = "worker_artifact" if matching_sensitive_registry else "cdp_run_session"
    source_session_id: str | None
    if matching_sensitive_registry:
        evidence = None
        source_session_id = run_session_id
        captured_frame = None
    else:
        evidence, source_session_id, _, captured_frame = await _read_run_session_page_evidence(
            ctx, run_session_id=run_session_id, current_url=current_url
        )
    # A capture that landed on a substituted session can still look usable (a blank replacement page
    # satisfies the schema check), and stamping it honestly would then refuse it, leaving the run with
    # nothing. The worker artifact is the run's own page, so prefer it over any foreign-session read.
    if (
        evidence is None
        or source_session_id != run_session_id
        or not _dispatched_terminal_page_evidence_is_usable(evidence)
    ):
        source = "worker_artifact"
        captured_frame = None
        source_session_id = run_session_id
        evidence = await _fetch_dispatched_terminal_page_evidence(
            run_id=run_id,
            organization_id=organization_id,
            current_url=current_url,
            workflow=workflow or ctx.last_workflow,
            origin_redaction_registry=origin_redaction_registry,
        )
    if evidence is None or not _dispatched_terminal_page_evidence_is_usable(evidence):
        return
    _, preserved_stored_evidence = store_post_run_page_evidence(
        ctx,
        evidence,
        run_id=run_id,
        current_url=current_url,
        source_browser_session_id=source_session_id,
        run_browser_session_id=run_session_id,
    )
    if (
        captured_frame is not None
        and not preserved_stored_evidence
        and not (
            origin_redaction_registry is not None
            and origin_redaction_registry.workflow_run_id == run_id
            and origin_redaction_registry.contains_sensitive_values
        )
    ):
        enqueue_screenshot(
            ctx,
            captured_frame.b64,
            provenance=ScreenshotProvenance(
                source_tool="dispatched_terminal_page_capture",
                captured_url=captured_frame.captured_url,
                observation_step=None,
                browser_session_id=captured_frame.browser_session_id,
                workflow_run_id=run_id,
                action_relation=ScreenshotActionRelation.WORKFLOW_RUN_RESULT,
                dispatch_url=captured_frame.dispatch_url,
                dispatch_browser_session_id=captured_frame.dispatch_browser_session_id,
                producer_browser_session_id=captured_frame.producer_browser_session_id,
                session_binding=captured_frame.session_binding,
            ),
            captured_at=captured_frame.captured_at,
        )
    LOG.info(
        "copilot_dispatched_terminal_page_evidence_captured",
        workflow_run_id=run_id,
        dispatch_to_worker=True,
        bounded_page_schema=has_bounded_page_schema(evidence),
        source=source,
        source_browser_session_id=source_session_id,
        stored=not preserved_stored_evidence,
    )


def _dispatched_terminal_page_evidence_is_usable(evidence: dict[str, Any]) -> bool:
    return has_bounded_page_schema(evidence) or bool(page_evidence_prose_text(evidence).strip())


def _ephemeral_input_values_by_parameter_key(
    code_artifact_metadata: object,
    scout_trajectory: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve private scout values only through an explicit model-submitted identity binding."""
    metadata_rows = code_artifact_metadata.values() if isinstance(code_artifact_metadata, Mapping) else ()
    values_by_input_id = {
        str(interaction.get("input_id") or "").strip(): interaction.get("input_value")
        for interaction in scout_trajectory
        if str(interaction.get("input_id") or "").strip() and isinstance(interaction.get("input_value"), str)
    }
    resolved: dict[str, Any] = {}
    for metadata in metadata_rows:
        if not isinstance(metadata, Mapping):
            continue
        bindings = metadata.get("input_bindings")
        if not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, Mapping):
                continue
            parameter_key = str(binding.get("parameter_key") or "").strip()
            input_id = str(binding.get("input_id") or "").strip()
            if parameter_key and input_id in values_by_input_id and parameter_key not in resolved:
                resolved[parameter_key] = values_by_input_id[input_id]
    return resolved


def _resolve_run_data_and_unbound_keys(
    all_workflow_params: Sequence[WorkflowParameter],
    user_params: Mapping[str, Any],
    *,
    ephemeral_input_values: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    data: dict[str, Any] = {}
    unbound: list[str] = []
    for wp in all_workflow_params:
        if wp.key in user_params:
            data[wp.key] = user_params[wp.key]
            continue
        if ephemeral_input_values is not None and wp.key in ephemeral_input_values:
            data[wp.key] = ephemeral_input_values[wp.key]
            continue
        if wp.default_value is not None and wp.default_value != "":
            data[wp.key] = wp.default_value
            continue
        # An at-will credential (credential_id type, no default) is optional for
        # the test run and therefore gets neither a placeholder nor an unbound marker.
        if wp.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID and wp.default_value is None:
            continue
        placeholder = _placeholder_for_parameter_type(wp.workflow_parameter_type)
        if placeholder is not None:
            data[wp.key] = placeholder
            LOG.info(
                "Auto-filled missing workflow parameter for copilot test run",
                parameter_key=wp.key,
                parameter_type=str(wp.workflow_parameter_type),
            )
        unbound.append(wp.key)
    return data, unbound


async def _bind_origin_run_redaction_registry(
    ctx: CopilotContext,
    *,
    workflow_run_id: str,
    parameter_values: Mapping[str, Any],
    credential_ids: Sequence[str],
    sensitive_parameter_keys: Sequence[str],
) -> OriginRunRedactionRegistry:
    # The model-disclosure registry is a sensitive-value scrub set, not a copy of the run request.
    # Persisted worker HTML retains the pre-existing, broader all-parameter redaction set without
    # registering capability references at the workflow write seam.
    serialized_artifact_parameters = app.AGENT_FUNCTION.serialize_codeblock_parameters(dict(parameter_values))
    sensitive_parameter_values = {
        key: parameter_values[key] for key in sensitive_parameter_keys if key in parameter_values
    }
    serialized = app.AGENT_FUNCTION.serialize_codeblock_parameters(sensitive_parameter_values)
    sensitive_values_complete = all(key in parameter_values and key in serialized for key in sensitive_parameter_keys)
    awaiting_runtime_secret_values = False
    credential_parameters: dict[str, Any] = {}
    for index, credential_id in enumerate(credential_ids):
        try:
            db_credential = await app.DATABASE.credentials.get_credential(
                credential_id,
                organization_id=ctx.organization_id,
            )
            if db_credential is None:
                sensitive_values_complete = False
                continue
            vault_type = db_credential.vault_type or CredentialVaultType.BITWARDEN
            credential_service = app.CREDENTIAL_VAULT_SERVICES.get(vault_type)
            if credential_service is None:
                sensitive_values_complete = False
                continue
            credential_item = await credential_service.get_credential_item(db_credential)
            credential_item = await app.AGENT_FUNCTION.process_registered_credential_item(
                workflow_run_id=workflow_run_id,
                db_credential=db_credential,
                credential_item=credential_item,
            )
            credential = credential_item.credential
            # TOTP routing metadata identifies a retrieval capability; it is not the runtime OTP.
            # Registering it as a secret value can corrupt ordinary generated code (for example,
            # an identifier named ``totp_input``) before the workflow write seam sees it.
            credential_parameters[f"copilot_run_credential_{index}"] = credential.model_dump(
                exclude_none=True,
                exclude={"totp_identifier", "totp_type"},
            )
            # A code block can mint or poll an OTP after parameter binding. Keep disclosure closed
            # until the terminal run context has supplied the exact values it actually generated.
            awaiting_runtime_secret_values = awaiting_runtime_secret_values or bool(
                getattr(db_credential, "totp_identifier", None)
                or getattr(credential, "totp", None)
                or getattr(credential, "totp_identifier", None)
            )
        except Exception:
            sensitive_values_complete = False
            LOG.warning(
                "Origin-run credential redaction registry could not be completed",
                workflow_run_id=workflow_run_id,
                credential_id=credential_id,
                exc_info=True,
            )
    if credential_parameters:
        serialized_credentials = app.AGENT_FUNCTION.serialize_codeblock_parameters(credential_parameters)
        if set(serialized_credentials) != set(credential_parameters):
            sensitive_values_complete = False
        serialized.update(serialized_credentials)
        serialized_artifact_parameters.update(serialized_credentials)
    registry = OriginRunRedactionRegistry(
        workflow_run_id=workflow_run_id,
        parameters=dict(serialized),
        contains_sensitive_values=bool(sensitive_parameter_keys or credential_ids),
        contains_all_sensitive_values=sensitive_values_complete and not awaiting_runtime_secret_values,
        contains_all_static_sensitive_values=sensitive_values_complete,
        awaiting_runtime_secret_values=awaiting_runtime_secret_values,
        artifact_parameters=dict(serialized_artifact_parameters),
    )
    ctx.origin_run_redaction_registry = registry
    if registry.contains_sensitive_values and not register_matching_origin_run_redaction_values(ctx, workflow_run_id):
        register_secret_scrub_values_from_structure(ctx, registry.parameters)
    return registry


async def _complete_origin_run_redaction_registry_from_runtime(
    ctx: CopilotContext,
    workflow_run_id: str,
) -> OriginRunRedactionRegistry | None:
    """Import the terminal run's exact runtime secrets before admitting structured page reads."""
    registry = getattr(ctx, "origin_run_redaction_registry", None)
    if registry is None or registry.workflow_run_id != workflow_run_id or not registry.awaiting_runtime_secret_values:
        return registry

    runtime_secret_values = await consume_copilot_runtime_secret_values(
        organization_id=ctx.organization_id,
        workflow_run_id=workflow_run_id,
    )
    if runtime_secret_values is None:
        return registry

    parameters = dict(registry.parameters)
    artifact_parameters = dict(registry.artifact_parameters or registry.parameters)
    if runtime_secret_values:
        parameters["copilot_run_runtime_secret_values"] = tuple(sorted(runtime_secret_values))
        artifact_parameters["copilot_run_runtime_secret_values"] = tuple(sorted(runtime_secret_values))
    completed = OriginRunRedactionRegistry(
        workflow_run_id=registry.workflow_run_id,
        parameters=parameters,
        contains_sensitive_values=registry.contains_sensitive_values,
        contains_all_sensitive_values=registry.contains_all_static_sensitive_values,
        contains_all_static_sensitive_values=registry.contains_all_static_sensitive_values,
        awaiting_runtime_secret_values=False,
        artifact_parameters=artifact_parameters,
    )
    ctx.origin_run_redaction_registry = completed
    register_matching_origin_run_redaction_values(ctx, workflow_run_id)
    return completed


def terminal_ready_for_latch(
    *,
    current_workflow_labels: list[str],
    has_executed_blocks: bool,
    unverified: list[str],
    composition_unverified: list[str],
    artifact_reason: object | None,
    structured_blocker: object | None,
    empty_data_blocks: object,
) -> bool:
    """The single definition of "tested". Offline replay calls this, so the rule cannot drift from its grader."""
    return (
        # "Every label is credited" says nothing when there are no labels, so an unresolvable
        # workflow must not satisfy it by emptiness.
        bool(current_workflow_labels)
        and has_executed_blocks
        and not unverified
        and not composition_unverified
        and artifact_reason is None
        and structured_blocker is None
        and not empty_data_blocks
    )


def _credit_composition_verified_labels(
    ctx: AgentContext,
    labels_to_execute: list[str],
    start_provenance: FrontierStartProvenance,
) -> None:
    """Passing is not anchoring: credit only a run that started from a provable composition state
    and executed exactly the workflow labels following the credit already earned."""
    if start_provenance == "unanchored":
        return
    workflow_labels = _current_workflow_block_labels(ctx)
    if not workflow_labels:
        return
    credited = list(ctx.composition_verified_labels or [])
    # Credit is an ordered contiguous prefix, so a workflow that no longer opens with it has to
    # re-earn the whole chain rather than keep set membership that says nothing about order.
    if credited != workflow_labels[: len(credited)]:
        ctx.composition_verified_labels = []
        return
    if not labels_to_execute:
        return
    # Starting before the credited boundary is more evidence, not less: a walk-back replay or a
    # full re-run proves the chain from further back, so credit it rather than demanding the plan
    # begin exactly where the previous credit stopped.
    try:
        start_index = workflow_labels.index(labels_to_execute[0])
    except ValueError:
        return
    if start_index > len(credited):
        return
    end = start_index + len(labels_to_execute)
    if labels_to_execute != workflow_labels[start_index:end]:
        return
    ctx.composition_verified_labels = workflow_labels[: max(end, len(credited))]


async def acquire_build_test_browser_session(ctx: CopilotContext, *, fresh: bool) -> dict[str, Any] | None:
    """The single initial-acquisition seam used by every build-test run."""
    if fresh:
        return await ensure_build_test_browser_session(ctx)
    return await verify_build_test_browser_session_by_attaching(
        ctx,
        copilot_chat_id=ctx.workflow_copilot_chat_id,
        copilot_turn_id=ctx.turn_id,
    )


def _with_build_test_acquisition_context(
    result: dict[str, Any], *, requested_block_labels: Sequence[str], fresh: bool
) -> dict[str, Any]:
    data = result.get("data")
    if not isinstance(data, dict):
        data = {}
        result["data"] = data
    data["requested_block_labels"] = list(requested_block_labels)
    data["executed_block_labels"] = []
    data["used_fresh_run_session"] = fresh
    return result


async def run_workflow_end_to_end(ctx: CopilotContext, workflow_yaml: str) -> dict[str, Any]:
    """Run every block of a candidate workflow in one browser minted for this run. Frontier
    selection is bypassed and the provenance stamped so a clean result earns composition credit
    for the whole chain rather than for one label."""
    authority_error = _authority_tool_error(ctx, "run_blocks_and_collect_debug")
    if authority_error:
        return {"ok": False, "error": authority_error}

    ctx.runner_code_block_associations_by_label = runner_code_block_associations(
        workflow_yaml,
        prior_associations=ctx.runner_code_block_associations_by_label,
        preserve_existing=True,
    )
    workflow = await _process_workflow_yaml(
        workflow_id=ctx.workflow_id,
        workflow_permanent_id=ctx.workflow_permanent_id,
        organization_id=ctx.organization_id,
        workflow_yaml=workflow_yaml,
        settings_fallback_yaml=ctx.staged_workflow_yaml or ctx.persisted_workflow_yaml,
        settings_fallback_workflow=ctx.staged_workflow,
    )
    # Checked before anything is staged: a blockless candidate would otherwise replace the good
    # in-memory workflow on its way to returning an error.
    labels = _workflow_definition_block_labels(workflow.workflow_definition)
    if not labels:
        return {"ok": False, "error": "This workflow has no blocks to run."}

    ctx.staged_workflow = workflow
    ctx.staged_workflow_yaml = workflow_yaml
    ctx.has_staged_proposal = True
    ctx.workflow_yaml = workflow_yaml
    ctx.last_workflow = workflow
    ctx.last_workflow_yaml = workflow_yaml

    # This run starts the chain over in its own browser, so its result supersedes credit earned
    # by earlier partial runs rather than appending to it. A resume id left by an earlier plan
    # would outrank force_fresh_session and run this in a carried browser, which is the one way
    # "initial" could be stamped on a start it cannot prove.
    ctx.frontier_resume_session_id = None
    ctx.composition_verified_labels = []
    ctx.frontier_start_provenance = "initial"
    result = await _run_blocks_and_collect_debug(
        {"block_labels": labels, "parameters": {}},
        ctx,
        labels_to_execute=labels,
        frontier_start_label=labels[0],
        force_fresh_session=True,
    )
    recorded_outcome = await _verify_and_record_run_blocks_result(ctx, result, time.monotonic())
    finalized = finalize_build_test_result(
        ctx,
        source_tool="run_blocks_and_collect_debug",
        result=result,
        recorded_outcome=recorded_outcome,
    )
    if not finalized.get("ok"):
        _carry_prior_attempt_change_identity_into_result(ctx, finalized.get("data"), "test_end_to_end")
    return finalized


async def _attach_post_run_browser_enrichment(
    ctx: CopilotContext,
    result_data: dict[str, Any],
    *,
    workflow: Workflow | None,
    workflow_run_id: str,
    run_block_rows: list[WorkflowRunBlock],
    results: list[dict[str, Any]],
    block_outputs_by_label: Mapping[str, Any],
    failed_block_code: str | None,
    run_session_id: str | None,
    dispatch_to_worker: bool,
    sensitive_origin_run: bool,
    run_ok: bool,
    used_fresh_run_session: bool,
    run_detached_from_chat: bool,
) -> tuple[str, dict[str, Any] | None]:
    """Probe the browser for post-run facts and stamp them onto an already-recorded run result.
    The outcome is what is protected, not these awaits: it is committed before this runs, so a probe
    the deadline cancels costs enrichment and never the record. Only the screenshot capture is
    guarded here; every other await propagates and fails the tool call."""
    # Dispatched runs: the worker owns the run session; do not touch it over CDP from the API.
    # The frontier's anchors and the page the run ended on both come from the rows the worker
    # persisted instead. A dispatched run has no page title to report.
    current_url, page_title, dispatched_end_url = await _resolve_post_run_page_info(
        ctx,
        run_block_rows=run_block_rows,
        dispatch_to_worker=dispatch_to_worker,
        sensitive_origin_run=sensitive_origin_run,
        run_session_id=run_session_id,
    )

    screenshot_b64: str | None = None
    # Dispatched runs: the worker owns the persistent browser session, so the API side must not
    # grab the live page over CDP. Their at-failure frames come from worker-persisted artifacts.
    if not dispatch_to_worker and not run_ok and run_session_id and not sensitive_origin_run:
        try:
            browser_state = await resolve_persistent_browser_state(
                session_id=run_session_id,
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

    locator_observations: list[AuthoredLocatorObservationRow] | None = None
    post_run_page_capture: BuildTestPacketPageCapture | None = None
    if (
        not dispatch_to_worker
        and run_session_id
        and _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER
        and not ctx.copilot_total_timeout_exceeded
    ):
        # Structured evidence is admitted through the origin registry scrubber. Pixel capture
        # and locator probes remain withheld for sensitive runs because they have no equivalent
        # exact-value disclosure boundary.
        _pin_pre_run_page_reference(ctx, workflow_run_id)
        post_run_page_capture = await _capture_and_store_post_run_page(
            ctx,
            run_session_id=run_session_id,
            run_id=workflow_run_id,
            current_url=current_url,
        )

    if not sensitive_origin_run:
        locator_observations = await _observe_authored_locators(
            ctx,
            run_session_id=run_session_id,
            failed_block_code=failed_block_code,
            worker_owned=dispatch_to_worker,
            observation_deadline_exceeded=ctx.copilot_total_timeout_exceeded,
        )

    if not dispatch_to_worker and not ctx.copilot_total_timeout_exceeded:
        await _capture_registered_artifact_evidence(
            ctx,
            run_id=workflow_run_id,
            organization_id=ctx.organization_id,
            downloaded_artifact_ids=_collect_downloaded_artifact_ids(block_outputs_by_label),
        )

    # Dispatched runs are worker-owned, so the API cannot CDP-capture the terminal page; read the
    # worker-persisted terminal HTML artifact instead and route it through the same post-run sink.
    if dispatch_to_worker and run_session_id and not ctx.copilot_total_timeout_exceeded:
        await _capture_dispatched_terminal_page_evidence(
            ctx,
            workflow=workflow,
            run_id=workflow_run_id,
            run_session_id=run_session_id,
            organization_id=ctx.organization_id,
            current_url=current_url,
            origin_redaction_registry=ctx.origin_run_redaction_registry,
        )

    result_data["current_url"] = current_url
    result_data["page_title"] = page_title
    if locator_observations is not None:
        result_data["authored_locator_observations"] = locator_observations
    if not dispatch_to_worker and current_url:
        result_data["current_url_live_observed"] = True
    if dispatch_to_worker and dispatched_end_url is None:
        result_data["current_url_evidence"] = NO_PERSISTED_END_URL
    post_run_page_evidence = _same_run_page_evidence_for_result(ctx, workflow_run_id)
    if post_run_page_evidence is not None:
        result_data["post_run_page_evidence"] = model_visible_composition_evidence(post_run_page_evidence)
    if post_run_page_capture is not None:
        result_data["post_run_page_capture"] = post_run_page_capture.model_dump(mode="json")
    _attach_run_session_facts(
        result_data,
        used_fresh_run_session=used_fresh_run_session,
        run_detached_from_chat=run_detached_from_chat,
        run_ok=run_ok,
        page_evidence=post_run_page_evidence,
    )
    resolved_screenshot_b64 = _resolve_run_screenshot_b64(live_capture=screenshot_b64, results=results, run_ok=run_ok)
    if resolved_screenshot_b64 is not None:
        result_data["screenshot_base64"] = resolved_screenshot_b64
    return current_url, post_run_page_evidence


async def _halt_turn_if_superseded(
    ctx: CopilotContext,
    run: WorkflowRun | None,
    *,
    workflow_run_id: str,
) -> None:
    """A run this turn dispatched that a newer test in the same chat cancelled is not a repairable
    failure, so the turn drains instead of handing the model a result to retry.

    The run row's reason is the fast path. Copilot's own record is consulted when it does not
    match, because the superseded run's finalizer can overwrite that reason with its own.
    """
    if run is None or workflow_run_id not in ctx.dispatched_run_ids_this_turn:
        return
    if run.failure_reason != SUPERSEDED_BY_NEWER_TEST_REASON:
        # A run that ended without a failure reason was never superseded, so the fallback read is
        # reserved for the one case that can have lost the marker: a run that ended badly.
        if not run.failure_reason or not ctx.workflow_copilot_chat_id:
            return
        if not await app.DATABASE.workflow_params.build_test_run_was_superseded(
            organization_id=ctx.organization_id,
            workflow_copilot_chat_id=ctx.workflow_copilot_chat_id,
            workflow_run_id=workflow_run_id,
        ):
            return
    stash_build_test_superseded_halt(ctx, workflow_run_id=workflow_run_id)


async def _run_blocks_and_collect_debug(
    params: dict[str, Any],
    ctx: CopilotContext,
    *,
    labels_to_execute: list[str] | None = None,
    block_outputs_to_seed: dict[str, Any] | None = None,
    frontier_start_label: str | None = None,
    force_fresh_session: bool = False,
) -> dict[str, Any]:
    if ctx.budget_expiry_state.drain_active:
        return dict(budget_run_denial(ctx.budget_expiry_state))

    # Older drafts predate server-owned block associations. Associate their current raw snapshot
    # once before outcome collection without adding anything to model-controlled YAML.
    if not ctx.runner_code_block_associations_by_label:
        ctx.runner_code_block_associations_by_label = runner_code_block_associations(
            ctx.staged_workflow_yaml or ctx.workflow_yaml
        )

    # Read the planner's session choice before any exit path, so a run that bails cannot leave it
    # set for a later run whose frontier was never proven against that browser.
    resume_session_id = ctx.frontier_resume_session_id
    ctx.frontier_resume_session_id = None
    start_provenance: FrontierStartProvenance = ctx.frontier_start_provenance or "unanchored"
    ctx.frontier_start_provenance = None

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
    ctx.last_run_blocks_block_ids = []
    ctx.last_run_blocks_block_labels = []
    # Verified state is NOT invalidated pre-run. On a failed / partial run we
    # want the prior verified prefix preserved so the next edit can still use
    # the optimization. YAML-diff-based invalidation for edited/downstream
    # labels happens in update_and_run_blocks_tool at edit time, which is the
    # right moment to drop stale outputs. Full success at the end of this
    # function updates verified state in place (overwriting re-run labels).

    snapshot = await _select_execution_snapshot(ctx, block_labels)
    if snapshot is None:
        return {"ok": False, "error": f"Workflow not found: {ctx.workflow_permanent_id}"}
    workflow = snapshot.workflow

    for label in block_labels:
        if not workflow.get_output_parameter(label):
            return {"ok": False, "error": f"Block label not found in saved workflow: {label!r}"}

    workflow_definition = workflow.workflow_definition
    finally_block_label = (
        workflow_definition.get("finally_block_label")
        if isinstance(workflow_definition, Mapping)
        else getattr(workflow_definition, "finally_block_label", None)
    )
    labels_that_may_execute = list(labels_to_execute)
    if (
        isinstance(finally_block_label, str)
        and finally_block_label
        and finally_block_label not in labels_that_may_execute
    ):
        # WorkflowService executes this top-level block after every non-canceled
        # body, independently of the partial-run block whitelist. Admission,
        # runtime code security, and credential replay checks must all see it.
        labels_that_may_execute.append(finally_block_label)

    runtime_security_failure = _runtime_code_security_failure_for_selected_labels(
        workflow,
        block_labels=list(block_labels),
        labels_to_execute=labels_that_may_execute,
        frontier_start_label=frontier_start_label,
    )
    if runtime_security_failure is not None:
        ctx.last_executed_block_labels = []
        return runtime_security_failure

    requires_sandbox = _selected_blocks_require_sandbox(
        _workflow_definition_blocks_for_code_security(workflow_definition),
        selected_labels=set(labels_that_may_execute),
    )

    tool_credential_ids = _extract_credential_ids_from_tool_value(params.get("parameters") or {})
    credential_ids = list(
        dict.fromkeys(
            tool_credential_ids + _extract_credential_ids_from_workflow_definition(workflow.workflow_definition)
        )
    )
    definition_credential_ids = _extract_credential_ids_for_labels(
        workflow.workflow_definition, labels_that_may_execute
    )
    approval_credential_ids = list(dict.fromkeys(tool_credential_ids + definition_credential_ids))
    admitted_sheet_ids = await _approve_server_verified_google_sheet_bindings(
        _google_sheet_connection_bindings_from_workflow_definition(
            workflow.workflow_definition,
            selected_labels=labels_that_may_execute,
        ),
        tool_activity=ctx.tool_activity,
        organization_id=ctx.organization_id,
        request_policy=ctx.request_policy,
    )
    non_sheet_credential_ids = set(tool_credential_ids) | set(
        _extract_credential_ids_for_labels(
            workflow.workflow_definition,
            labels_that_may_execute,
            excluded_block_types={"google_sheets_read", "google_sheets_write"},
        )
    )
    dispatch_scoped_sheet_ids = set(admitted_sheet_ids) - non_sheet_credential_ids
    google_approval_blocker = _credential_run_approval_blocker_signal(
        approval_credential_ids,
        ctx.request_policy,
        additional_approved_ids=dispatch_scoped_sheet_ids,
        google_reference_ids=_google_connection_reference_ids(workflow.workflow_definition, labels_that_may_execute),
    )
    if google_approval_blocker is not None:
        ctx.connected_account_recovery_choices = (
            await _server_verified_google_account_choices(ctx.organization_id) or []
        )
        tool_error = stash_blocker_signal(ctx, google_approval_blocker)
        stash_turn_halt_from_blocker_signal(ctx, google_approval_blocker, source="run_execution")
        return {"ok": False, "error": tool_error}
    credential_approval_error = _credential_run_approval_error(
        approval_credential_ids,
        ctx.request_policy,
        additional_approved_ids=dispatch_scoped_sheet_ids,
    )
    if credential_approval_error is not None:
        return {"ok": False, "error": credential_approval_error}

    credential_error = await _credential_ids_validation_error(credential_ids, ctx)
    if credential_error is not None:
        return {"ok": False, "error": credential_error}

    from skyvern.forge.sdk.schemas.organizations import Organization
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
    from skyvern.services import workflow_service

    org = await app.DATABASE.organizations.get_organization(organization_id=ctx.organization_id)
    if not org:
        return {"ok": False, "error": "Organization not found"}

    organization = Organization.model_validate(org)
    # Retain the per-org rollout, but never use its negative decision as permission to execute
    # workflow code in the API process.
    dispatch_to_worker = await app.AGENT_FUNCTION.should_dispatch_copilot_block_run_to_worker(
        organization_id=ctx.organization_id,
        workflow_permanent_id=ctx.workflow_permanent_id,
    )
    # Compared against the literal True so anything other than an explicit opt-in — including a
    # test double that auto-mocks the hook into a truthy object — still fails closed.
    allow_inline_code_execution = (
        app.AGENT_FUNCTION.allow_copilot_inline_code_execution() is True if not dispatch_to_worker else False
    )
    if requires_sandbox and not dispatch_to_worker and not allow_inline_code_execution:
        return _copilot_sandbox_unavailable_result(
            organization_id=ctx.organization_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
        )

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

    # Short-circuit before a wasted workflow execution when the definition
    # JSON has drifted from the persisted parameter rows that runtime reads.
    invariant_error = (
        None
        if snapshot.provenance == "staged"
        else _parameter_binding_invariant_error(
            workflow,
            list(snapshot.workflow_parameters),
            list(snapshot.output_parameters),
        )
    )
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

    # A resume proven against another browser has to run in that browser; minting or falling back
    # to the chat's would drop the very state the resume was authorised against.
    use_fresh_session = resume_session_id is None and (
        force_fresh_session or _should_use_fresh_session_for_login_first_replay(ctx, labels_that_may_execute, workflow)
    )
    # Reported as run evidence, so it stays literal: a browser minted for this run. A carried
    # browser is not one, and reporting it as such would misattribute a challenge that stalled.
    used_fresh_run_session = False
    # Whether the run executed outside the chat's browser at all, by either route. This is what
    # gates the post-run rebind and the pane association, neither of which cares which route.
    run_detached_from_chat = False
    debug_session_id: str | None = None

    # Without a session, the workflow service launches the browser in-process,
    # which only works in worker pods (cloakbrowser isn't in the API image).
    if use_fresh_session:
        # The scout authenticated its debug session, so replaying the login-first
        # synthesized block into it meets a rehydrated authenticated view and the
        # login fill() waits out its full element timeout. Mint a fresh session for
        # this run only, then restore the scout's debug session as the context
        # session so the rest of the turn (scouting, narration, SKY-9328 reuse)
        # keeps it; the fresh id is threaded into the run calls explicitly.
        debug_session_id = ctx.browser_session_id
        ctx.browser_session_id = None
        session_err = await acquire_build_test_browser_session(ctx, fresh=True)
        if session_err is not None:
            ctx.browser_session_id = debug_session_id
            return _with_build_test_acquisition_context(session_err, requested_block_labels=block_labels, fresh=True)
        run_session_id = ctx.browser_session_id
        ctx.browser_session_id = debug_session_id
        used_fresh_run_session = True
        run_detached_from_chat = True
        LOG.info(
            "copilot_login_replay_fresh_session_minted",
            labels_to_execute=labels_to_execute,
            frontier_start_label=frontier_start_label,
            run_session_id=run_session_id,
            debug_session_id=debug_session_id,
        )
    elif resume_session_id is not None and resume_session_id != ctx.browser_session_id:
        # Treated like a minted session from here: whatever browser the chat holds stays its own,
        # and the pane follows this run through the same association a minted one publishes.
        debug_session_id = ctx.browser_session_id
        run_session_id = resume_session_id
        run_detached_from_chat = True
        LOG.info(
            "copilot_frontier_resume_session_carried",
            labels_to_execute=labels_to_execute,
            frontier_start_label=frontier_start_label,
            run_session_id=run_session_id,
            debug_session_id=debug_session_id,
        )
    else:
        # This id is dispatched into a workflow run without ever being attached here, so an
        # unverified session cannot be discovered later and must fail now instead.
        session_err = await acquire_build_test_browser_session(ctx, fresh=False)
        if session_err is not None:
            return _with_build_test_acquisition_context(session_err, requested_block_labels=block_labels, fresh=False)
        run_session_id = ctx.browser_session_id

    seeded_runtime_workflow = await _workflow_with_runtime_frontier_starter_url_seed(
        runtime_workflow,
        ctx,
        labels_to_execute=labels_to_execute,
        runtime_frontier_anchor_url=runtime_frontier_anchor_url,
        session_id_override=run_session_id,
    )
    runtime_frontier_starter_url_seeded = seeded_runtime_workflow is not runtime_workflow
    runtime_workflow = seeded_runtime_workflow

    requested_completion_contract = _requested_completion_contract(ctx, runtime_workflow, labels_to_execute)
    if requested_completion_contract is not None:
        runtime_workflow = runtime_workflow.model_copy(
            update={
                "workflow_definition": runtime_workflow.workflow_definition.model_copy(
                    update={"completion_contract": requested_completion_contract}
                )
            }
        )

    # Snapshot version persisted for a worker-dispatched run or an inline run of a definition that
    # was never persisted. The run is created against its exact workflow_id so prepare_workflow
    # reads parameter rows from the same definition execute_workflow receives. Without the inline
    # snapshot, newly drafted parameters exist only in memory and are omitted from the
    # WorkflowRunParameter rows, causing block execution to fail before it reaches the browser.
    # The snapshot is soft-deleted once the run resolves so it never lingers as the latest version.
    dispatch_draft_workflow_id: str | None = None
    # The persisted dispatch version (its own regenerated parameter ids) used for post-run output
    # mapping on the dispatch path; runtime_workflow / ctx.staged_workflow is left unmutated.
    dispatch_workflow: Workflow | None = None
    if dispatch_to_worker or snapshot.provenance == "staged" or runtime_workflow != snapshot.workflow:
        # Persist the wrapped runtime workflow as a real new version (with its own parameter /
        # output-parameter rows) through the normal create machinery. The run is then created
        # against this version so the worker resolves it by run.workflow_id and registers block
        # outputs from the version's own rows.
        try:
            dispatch_workflow = await app.WORKFLOW_SERVICE.create_copilot_dispatch_draft_version(
                runtime_workflow=runtime_workflow,
                organization_id=ctx.organization_id,
            )
            dispatch_draft_workflow_id = dispatch_workflow.workflow_id
        except Exception:
            LOG.warning(
                "Failed to persist copilot run snapshot; blocking execution",
                organization_id=ctx.organization_id,
                workflow_permanent_id=ctx.workflow_permanent_id,
                dispatch_to_worker=dispatch_to_worker,
                snapshot_provenance=snapshot.provenance,
                exc_info=True,
            )
            if dispatch_to_worker:
                return _copilot_sandbox_unavailable_result(
                    organization_id=ctx.organization_id,
                    workflow_permanent_id=ctx.workflow_permanent_id,
                )
            return {
                "ok": False,
                "error": "Unable to prepare the Copilot test-run snapshot; execution was not started.",
            }

    if dispatch_workflow is not None:
        snapshot = _materialized_execution_snapshot(snapshot, dispatch_workflow)

    all_workflow_params = list(snapshot.workflow_parameters)
    all_output_params = list(snapshot.output_parameters)

    ephemeral_input_values = _ephemeral_input_values_by_parameter_key(
        ctx.code_artifact_metadata,
        ctx.scout_trajectory,
    )
    data, ctx.unbound_required_parameter_keys = _resolve_run_data_and_unbound_keys(
        all_workflow_params,
        user_params,
        ephemeral_input_values=ephemeral_input_values,
    )

    workflow_request = WorkflowRequestBody(
        data=data if data else None,
        browser_session_id=run_session_id,
        # Copilot test runs don't need scrolling post-action screenshots;
        # the ForgeAgent's split screenshots (used for LLM context) are unaffected.
        max_screenshot_scrolls=0,
    )

    # run_task is the in-process inline execution task, only ever set on the dev-only inline path.
    # For dispatched runs it stays None: the worker owns execution and the watchdog observes purely
    # via DB polling.
    run_task: asyncio.Task | None = None
    sensitive_run_custody_lock: asyncio.Lock | None = None
    sensitive_run_session_id: str | None = None
    interim_run_id: str | None = None
    # Set only where the run may already be executing, so an unwind before either executor call
    # can still retract the run-start record.
    run_submission_attempted = False
    try:
        workflow_run = await workflow_service.prepare_workflow(
            workflow_id=ctx.workflow_permanent_id,
            organization=organization,
            workflow_request=workflow_request,
            template=False,
            # Both execution paths pin the exact version. A permanent-id lookup could
            # select a newer proposal while this run still carries the previous definition.
            resolved_workflow_id=snapshot.workflow.workflow_id,
            max_steps=None,
            request_id=None,
            # The trigger type (and the -ui queue routing it implies) is a cloud contract; ask the
            # AgentFunction for it rather than hardcoding "manual == -ui pool" in OSS. OSS base
            # returns None (no routing hint); cloud returns the value its executor routes to -ui.
            trigger_type=(app.AGENT_FUNCTION.resolve_copilot_dispatch_trigger_type() if dispatch_to_worker else None),
            copilot_session_id=ctx.workflow_copilot_chat_id,
        )

        ctx.dispatched_run_ids_this_turn.add(workflow_run.workflow_run_id)

        # The ordinary Track-A producer owns this registry at run creation. Keeping the run id and
        # serialized values in one immutable carrier prevents a later context from vouching for it.
        origin_registry = await _bind_origin_run_redaction_registry(
            ctx,
            workflow_run_id=workflow_run.workflow_run_id,
            parameter_values=data,
            credential_ids=approval_credential_ids,
            sensitive_parameter_keys=_sensitive_parameter_keys_requiring_resolved_values(snapshot.workflow),
        )
        if origin_registry.contains_sensitive_values and run_session_id:
            sensitive_run_custody_lock = browser_page_custody_lock(ctx, session_id=run_session_id)
            await sensitive_run_custody_lock.acquire()
            sensitive_run_session_id = run_session_id
            record_sensitive_origin_run_taint(
                ctx, workflow_run_id=workflow_run.workflow_run_id, session_id=run_session_id
            )
            register_sensitive_origin_run_lease(
                ctx, workflow_run_id=workflow_run.workflow_run_id, session_id=run_session_id
            )

        # From here blocks execute and the browser moves, so the pages recorded before this run no
        # longer say where it is. Dropped now rather than on the way out, because the watchdog and
        # cancellation exits leave by paths a success-only reset never reaches.
        _forget_browser_position(ctx)

        # A run that never returns a result must not read as zero blocks at the terminal;
        # the tool-return record supersedes this one.
        interim_run_id = workflow_run.workflow_run_id
        ctx.terminal_envelope_run_outcomes.append(interim_run_start_outcome(interim_run_id))
        await _send_run_started_update(ctx, workflow_run.workflow_run_id)

        if dispatch_to_worker:
            # Submit through the cloud executor (Temporal). The run was created against the
            # snapshot version, so the worker resolves the exact wrapped definition via
            # run.workflow_id — no workflow_override crosses the wire. block_labels/block_outputs
            # and the shared browser session reproduce the frontier re-run on the worker.
            run_submission_attempted = True
            await AsyncExecutorFactory.get_executor().execute_workflow(
                request=None,
                background_tasks=None,
                organization=organization,
                workflow_id=workflow_run.workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_permanent_id=ctx.workflow_permanent_id,
                max_steps_override=None,
                api_key="copilot-agent",
                browser_session_id=run_session_id,
                block_labels=labels_to_execute,
                block_outputs=block_outputs_to_seed or None,
            )
        else:
            if allow_inline_code_execution:
                LOG.error(
                    "UNSANDBOXED: executing copilot workflow code in the API process because "
                    "COPILOT_ALLOW_INLINE_CODE_EXECUTION is enabled. This is a local-development path "
                    "with no sandbox isolation; the run below is NOT a sandboxed run.",
                    workflow_run_id=workflow_run.workflow_run_id,
                    workflow_permanent_id=ctx.workflow_permanent_id,
                    organization_id=ctx.organization_id,
                )
            # prepare_workflow replaced the ambient context with this run's own, so the marker is
            # scoped to this run and is inherited by the execution task created below.
            inline_run_context = skyvern_context.current()
            if inline_run_context is not None:
                inline_run_context.copilot_inline_execution = True

            inline_fence_failure = _inline_sequential_credential_fence_failure(
                workflow_run_id=workflow_run.workflow_run_id,
                sequential_credential_id=workflow_run.sequential_credential_id,
                dispatch_to_worker=dispatch_to_worker,
                block_labels=block_labels,
                labels_to_execute=labels_to_execute,
                frontier_start_label=frontier_start_label,
            )
            if inline_fence_failure is not None:
                _forget_interim_run_start(ctx, workflow_run.workflow_run_id)
                await app.WORKFLOW_SERVICE.mark_workflow_run_as_failed_if_not_final(
                    workflow_run_id=workflow_run.workflow_run_id,
                    failure_reason=inline_fence_failure["error"],
                )
                return inline_fence_failure

            await initialize_skyvern_state_file(
                workflow_run_id=workflow_run.workflow_run_id,
                organization_id=ctx.organization_id,
            )

            run_submission_attempted = True
            run_task = asyncio.create_task(
                app.WORKFLOW_SERVICE.execute_workflow(
                    workflow_run_id=workflow_run.workflow_run_id,
                    api_key="copilot-agent",
                    organization=organization,
                    browser_session_id=run_session_id,
                    block_labels=labels_to_execute,
                    block_outputs=block_outputs_to_seed or None,
                    # The run was created against the dispatch version, so execute that same
                    # definition: its blocks carry the persisted output-parameter rows. Executing
                    # the in-memory runtime copy instead registers block outputs against ids that
                    # were never persisted, and every consumer keyed on the definition drops them.
                    workflow_override=snapshot.workflow if dispatch_workflow is not None else runtime_workflow,
                    requested_completion_contract=requested_completion_contract,
                )
            )
    except BaseException:
        # Run setup / submission failed OR the tool was cancelled after the dispatch version was
        # created. The watchdog cleanup below never runs on this path, so soft-delete the version
        # here so it does not linger as the latest-by-permanent-id pointer. Catch BaseException so
        # asyncio.CancelledError (the SDK tool timeout) also cleans the draft up.
        if dispatch_draft_workflow_id is not None:
            await _delete_dispatch_draft(dispatch_draft_workflow_id, ctx.organization_id)
            dispatch_draft_workflow_id = None
        if sensitive_run_custody_lock is not None and sensitive_run_custody_lock.locked():
            if sensitive_run_session_id is not None:
                release_sensitive_origin_run_lease(ctx, workflow_run_id=workflow_run.workflow_run_id)
            sensitive_run_custody_lock.release()
        if interim_run_id is not None and not run_submission_attempted:
            _forget_interim_run_start(ctx, interim_run_id)
        raise

    active_run_association: ActiveRunSessionAssociation | None = None
    run_paused = False
    if run_detached_from_chat and debug_session_id and run_session_id:
        try:
            active_run_association = await publish_active_run_session(
                organization_id=ctx.organization_id,
                workflow_permanent_id=ctx.workflow_permanent_id,
                debug_browser_session_id=debug_session_id,
                run_browser_session_id=run_session_id,
                workflow_run_id=workflow_run.workflow_run_id,
                turn_id=ctx.turn_id,
            )
        except Exception:
            LOG.warning(
                "Failed to publish active Copilot run session",
                organization_id=ctx.organization_id,
                workflow_permanent_id=ctx.workflow_permanent_id,
                workflow_run_id=workflow_run.workflow_run_id,
                debug_browser_session_id=debug_session_id,
                run_browser_session_id=run_session_id,
                exc_info=True,
            )

    try:
        # The OpenAI Agents SDK wraps this tool in
        # ``asyncio.wait_for(..., timeout=RUN_BLOCKS_SAFETY_CEILING_SECONDS)``, so
        # the poll loop leaves 10 s of headroom for the cancel-drain and
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
        budget_seconds = max(1, RUN_BLOCKS_SAFETY_CEILING_SECONDS - 10)

        # Mid-tool narrator bridge: feed block-status changes and step-level
        # heartbeats into NarratorState so the narration ticker keeps emitting
        # while a long workflow run is in flight.
        narrator_state: NarratorState | None = getattr(ctx, "narrator_state", None)
        narrator_enabled = narrator_state is not None and narration_handler_available()
        seen_block_states: dict[str, str] = {}
        prior_block_ts: datetime | None = initial_block_ts
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
                        block_run_identity_map=ctx.block_run_identity_map,
                        workflow_run_id=workflow_run.workflow_run_id,
                    )
                    prior_block_ts = tick_result.prior_block_ts
                    last_block_fetch_monotonic = tick_result.last_block_fetch_monotonic

                if run and WorkflowRunStatus(run.status).is_final():
                    final_status = run.status
                    exit_reason = "success"
                    break

                if run_task is not None and run_task.done():
                    # Row not terminal yet — shared reconcile path below flips
                    # most of these back to success after post-drain reread.
                    # Dispatched runs have no in-process task, so loop exit is anchored purely
                    # on the DB-terminal status check above.
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

                if is_paused:
                    exit_reason = "paused"
                    run_paused = True
                    break

                if now - started_monotonic >= budget_seconds:
                    exit_reason = "ceiling"
                    break

            if exit_reason is not None and exit_reason not in ("success", "paused"):
                # A paused run is waiting for a person, so it is deliberately excluded here:
                # cancelling it would destroy the very state the person was asked to act on.
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
                    if pre_cancel_run is not None:
                        run = pre_cancel_run
                    if run is None or not WorkflowRunStatus(run.status).is_final():
                        if run_task is not None:
                            await _cancel_run_task_if_not_final(run_task, workflow_run.workflow_run_id)
                        else:
                            # Dispatched run — cooperative DB cancel so the worker stops.
                            await _cooperative_cancel_dispatched_run(workflow_run.workflow_run_id)
                        run_cancelled_by_watchdog = True
                        run = await _safe_read_workflow_run(
                            workflow_run.workflow_run_id, ctx.organization_id, context="post-drain"
                        )
                    trusted = _trusted_post_drain_status(run)
                    if trusted is not None:
                        final_status = trusted
                        exit_reason = "success"

            await _halt_turn_if_superseded(
                ctx,
                run,
                workflow_run_id=workflow_run.workflow_run_id,
            )

            if exit_reason != "success":
                assert exit_reason is not None  # narrows for mypy; outer check excludes "success" but not None
                error_msg = await _watchdog_error_message(
                    exit_reason, ctx, workflow_run.workflow_run_id, run, budget_seconds, dispatch_to_worker
                )
                user_facing_summary = _watchdog_user_facing_summary(exit_reason, budget_seconds, run)
                # Dispatched runs: the worker owns the run session, so do not attach to it over CDP.
                sensitive_origin_run = _origin_registry_contains_sensitive_values(
                    ctx.origin_run_redaction_registry,
                    workflow_run.workflow_run_id,
                )
                current_url, page_title = (
                    ("", "")
                    if dispatch_to_worker or sensitive_origin_run
                    else await _fallback_page_info(ctx, session_id_override=run_session_id)
                )
                result: dict[str, Any] = {
                    "ok": False,
                    "error": error_msg,
                    "data": {
                        "workflow_run_id": workflow_run.workflow_run_id,
                        "overall_status": run.status if run is not None else None,
                        "failure_reason": user_facing_summary,
                        "current_url": current_url,
                        "current_url_live_observed": bool(current_url) and not dispatch_to_worker,
                        "page_title": page_title,
                        # Omitting this reads downstream as "run session unknown", which grants a
                        # scout-sourced page post-run identity on exactly the fresh-session path.
                        "browser_session_id": run_session_id,
                        "blocks": await _recorded_watchdog_block_receipts(
                            workflow_run.workflow_run_id,
                            ctx.organization_id,
                        ),
                    },
                }
                _attach_run_session_facts(
                    result["data"],
                    used_fresh_run_session=used_fresh_run_session,
                    run_detached_from_chat=run_detached_from_chat,
                    run_ok=False,
                    page_evidence=_same_run_page_evidence_for_result(ctx, workflow_run.workflow_run_id),
                )
                result["data"]["control_signal"] = {
                    "kind": f"watchdog_{exit_reason}",
                    "user_facing_summary": user_facing_summary,
                }
                result["data"]["user_facing_summary"] = user_facing_summary
                if run_cancelled_by_watchdog:
                    result[_INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY] = True
                failed_result = _first_failed_result(result["data"]["blocks"])
                watchdog_locator_observations = (
                    None
                    if sensitive_origin_run
                    else await _observe_authored_locators(
                        ctx,
                        run_session_id=run_session_id,
                        failed_block_code=_failed_block_code(workflow, failed_result),
                        worker_owned=dispatch_to_worker,
                        observation_deadline_exceeded=ctx.copilot_total_timeout_exceeded,
                    )
                )
                if watchdog_locator_observations is not None:
                    result["data"]["authored_locator_observations"] = watchdog_locator_observations
                return result
        except asyncio.CancelledError:
            # A pause is detected several awaits before the result is returned, so a tool timeout
            # landing in that window reaches here with the run alive and waiting on a person.
            # Cancelling it would destroy the state the person was asked to act on, so leave the
            # run to the ``finally`` below, which adopts the executor instead.
            if run_paused:
                raise
            # The SDK's @function_tool(timeout=...) cancelled us mid-poll. Shield
            # the cleanup so the parent cancellation can't interrupt it mid-await.
            # If the shield itself is cancelled, fall back to a detached task
            # that outlives tool teardown and still reconciles workflow state.
            cancel_cleanup = (
                _cancel_run_task_if_not_final(run_task, workflow_run.workflow_run_id)
                if run_task is not None
                # Dispatched run: no in-process task, cooperatively flip the DB status instead.
                else _cooperative_cancel_dispatched_run(workflow_run.workflow_run_id)
            )
            try:
                await asyncio.shield(cancel_cleanup)
            except asyncio.CancelledError:
                fallback_cleanup = (
                    _cancel_run_task_if_not_final(run_task, workflow_run.workflow_run_id)
                    if run_task is not None
                    else _cooperative_cancel_dispatched_run(workflow_run.workflow_run_id)
                )
                fallback = asyncio.ensure_future(fallback_cleanup)
                _DETACHED_CLEANUP_TASKS.add(fallback)
                fallback.add_done_callback(_DETACHED_CLEANUP_TASKS.discard)
                fallback.add_done_callback(_log_detached_cleanup_failure)
            raise
        finally:
            # If any exit path above missed a cancel — e.g. an unexpected exception bubbling out of the
            # poll loop — signal the run_task so we don't leak it. Dispatched runs have no in-process
            # task, so there is nothing to signal.
            if run_task is not None and not run_task.done():
                if run_paused:
                    # The inline executor coroutine is what observes the approval and resumes the
                    # run, so it has to outlive this tool call instead of being cancelled.
                    detached_task = (
                        asyncio.create_task(
                            _retire_snapshot_after_execution(
                                run_task, dispatch_draft_workflow_id, workflow_run.workflow_run_id, ctx.organization_id
                            )
                        )
                        if dispatch_draft_workflow_id is not None
                        else run_task
                    )
                    _DETACHED_CLEANUP_TASKS.add(detached_task)
                    detached_task.add_done_callback(_DETACHED_CLEANUP_TASKS.discard)
                    detached_task.add_done_callback(_log_detached_cleanup_failure)
                else:
                    run_task.cancel()
            # Soft-delete the pinned draft so it never lingers as the latest version. Gated on a final
            # run state: on the normal path the poll loop only exits once the run is terminal, but an
            # unexpected exception can reach here before the worker has loaded the draft, and deleting
            # it then would 404 the worker's get_workflow(run.workflow_id). Runs on every exit path
            # (success fall-through, failure return, cancel raise).
            if dispatch_draft_workflow_id is not None:
                await _delete_dispatch_draft_if_run_final(
                    dispatch_draft_workflow_id, workflow_run.workflow_run_id, ctx.organization_id
                )

        # Skip the rebind when the run used a browser other than the chat's, so the chat's stays
        # the context session for the rest of the turn.
        if not run_detached_from_chat and run and run.browser_session_id:
            ctx.browser_session_id = run.browser_session_id

        blocks = await app.DATABASE.observer.get_workflow_run_blocks(
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=ctx.organization_id,
        )

        results = []
        block_outputs_by_label: dict[str, Any] = {}
        for block in blocks:
            block_result = _recorded_run_block_result(block)
            if hasattr(block, "output") and block.output:
                block_result["extracted_data"] = block.output
                if block.label is not None:
                    block_outputs_by_label[block.label] = block.output
            results.append(block_result)

        # Repository returns DESC by created_at; reverse for chronological order.
        run_block_rows = list(reversed(blocks))
        ctx.last_run_blocks_block_ids = list(
            dict.fromkeys(block.workflow_run_block_id for block in run_block_rows if block.workflow_run_block_id)
        )
        ctx.last_run_blocks_block_labels = list(dict.fromkeys(block.label for block in run_block_rows if block.label))

        await _attach_action_traces(blocks, results, ctx.organization_id, include_completed=True)
        await _complete_origin_run_redaction_registry_from_runtime(ctx, workflow_run.workflow_run_id)
        recorded_origin_registry = ctx.origin_run_redaction_registry
        sensitive_origin_run = _origin_registry_contains_sensitive_values(
            recorded_origin_registry,
            workflow_run.workflow_run_id,
        )
        if not sensitive_origin_run:
            await _attach_failed_block_screenshots(blocks, results, ctx.organization_id)

        # final_status is guaranteed set here: every non-success exit returns
        # above, and the success path always populates final_status.
        assert final_status is not None
        run_ok = WorkflowRunStatus(final_status) == WorkflowRunStatus.completed

        action_observations = _retained_action_observations(results)

        action_trace_summary: list[str] = []
        first_failed = _first_failed_result(results)
        failing_code_line: int | None = None
        if first_failed is not None:
            action_trace_summary = _failure_action_trace_summary(first_failed)
            failing_code_line = _failing_code_line(first_failed.get("action_trace"))
        failed_block_code = _failed_block_code(snapshot.workflow, first_failed)

        # Per-block action_trace is for derivation only — keep it out of the
        # compact packet. get_run_results remains the heavier inspection path.
        for entry in results:
            entry.pop("action_trace", None)

        block_end_urls = {} if sensitive_origin_run else _block_end_urls_by_label(run_block_rows)

        result_data: dict[str, Any] = {
            "workflow_run_id": workflow_run.workflow_run_id,
            "workflow_id": snapshot.workflow.workflow_id,
            "browser_session_id": run_session_id,
            "overall_status": final_status,
            "requested_block_labels": list(block_labels),
            "executed_block_labels": list(labels_to_execute),
            "frontier_start_label": frontier_start_label,
            "blocks": results,
            "action_trace_summary": action_trace_summary,
            "failing_code_line": failing_code_line,
            "action_observations": action_observations,
        }
        if runtime_frontier_anchor_url is not None:
            result_data["runtime_frontier_anchor_url"] = runtime_frontier_anchor_url
        if runtime_frontier_starter_url_seeded:
            result_data["runtime_frontier_starter_url_seeded"] = True
        if not run_ok and run and getattr(run, "failure_reason", None):
            result_data["failure_reason"] = run.failure_reason
        if not run_ok and run and getattr(run, "failure_category", None):
            result_data["failure_category"] = run.failure_category

        output_identity_workflow = snapshot.workflow
        result_data["requested_output_parameter_definitions"] = _requested_output_parameter_definitions(
            workflow_run_id=workflow_run.workflow_run_id,
            workflow=output_identity_workflow,
        )

        registered_outputs_by_label = await _attach_registered_output_parameter_values(
            workflow_run_id=workflow_run.workflow_run_id,
            workflow=output_identity_workflow,
            data=result_data,
            persisted_output_parameters=all_output_params,
        )
        # The run's terminal facts are known here, so the outcome is committed before any
        # browser-dependent probe: a probe the turn deadline cancels must not erase what the run did.
        # The post-run capture that drops another run's page now happens after the record, and the
        # anti-bot read has no run-id check of its own, so stale evidence would grade this run.
        if not post_run_inspection_cleanly_matches(ctx.composition_page_evidence, workflow_run.workflow_run_id):
            ctx.composition_page_evidence = None

        for label, output in registered_outputs_by_label.items():
            if isinstance(output, dict) and output:
                block_outputs_by_label[label] = output

        # The record below derives "fully tested" from these credits and prefers its own
        # extracted_data for terminal replies, so both have to land before it, not after.
        run_fully_completed = run_ok and all(r.get("status") == "completed" for r in results)
        if run_fully_completed:
            existing_prefix = list(ctx.verified_prefix_labels or [])
            existing_set = set(existing_prefix)
            for label in labels_to_execute:
                if label not in existing_set:
                    existing_prefix.append(label)
                    existing_set.add(label)
            ctx.verified_prefix_labels = existing_prefix
            _credit_composition_verified_labels(ctx, labels_to_execute, start_provenance)

        response = build_run_blocks_response(run_ok, result_data)
        committed_run_outcome = _commit_run_blocks_record(ctx, response)

        current_url, post_run_page_evidence = await _attach_post_run_browser_enrichment(
            ctx,
            result_data,
            workflow=snapshot.workflow,
            workflow_run_id=workflow_run.workflow_run_id,
            run_block_rows=run_block_rows,
            results=results,
            block_outputs_by_label=block_outputs_by_label,
            failed_block_code=failed_block_code,
            run_session_id=run_session_id,
            dispatch_to_worker=dispatch_to_worker,
            sensitive_origin_run=sensitive_origin_run,
            run_ok=run_ok,
            used_fresh_run_session=used_fresh_run_session,
            run_detached_from_chat=run_detached_from_chat,
        )
        settle_terminal_challenge_after_enrichment(ctx, response)
        settled_page_evidence = post_run_page_evidence or ctx.composition_page_evidence
        bind_post_run_page_evidence(
            ctx,
            result_data,
            # Singular selectors are stripped at this boundary; page_evidence_refs reaches a prompt.
            model_visible_composition_evidence(settled_page_evidence) if settled_page_evidence else None,
            regraded=_build_recorded_build_test_outcome(
                ctx,
                response,
                ctx.last_run_outcome or committed_run_outcome,
                response.get(_INTERNAL_GOAL_PATH_OMISSIONS_KEY),
            ),
        )
        _update_verification_evidence_from_run_result(ctx, response)

        # Update verified prefix state ONLY on a fully-successful run. A failed
        # suffix run leaves the browser in post-failure state, so we must not
        # trust blocks that individually succeeded inside it.
        if run_fully_completed:
            for label, output in block_outputs_by_label.items():
                ctx.verified_block_outputs[label] = output
            if sensitive_origin_run:
                # Preserve verified labels and outputs, but never carry browser-position URLs from
                # a credential-bearing run into a later frontier or model-visible result.
                _forget_browser_position(ctx)
                ctx.verified_prefix_current_url = None
            else:
                # Rebuilt from this run's rows alone: the position was forgotten at dispatch, and the
                # browser these pages describe is the one this run used.
                ctx.verified_prefix_block_end_urls = dict(block_end_urls)
                ctx.verified_prefix_block_end_session_id = run_session_id
                ctx.verified_prefix_terminal_label = run_block_rows[-1].label if run_block_rows else None
                verified_current_url = _valid_runtime_anchor_url(current_url)
                if verified_current_url is not None:
                    ctx.verified_prefix_current_url = verified_current_url

        return response
    finally:
        # A paused run keeps its association so the pane still follows the run the person was
        # asked to act on; the next run's generation replaces it.
        if active_run_association is not None and not run_paused:
            try:
                await clear_active_run_session(
                    organization_id=active_run_association.organization_id,
                    debug_browser_session_id=active_run_association.debug_browser_session_id,
                    generation=active_run_association.generation,
                )
            except Exception:
                LOG.warning(
                    "Failed to clear active Copilot run session",
                    organization_id=active_run_association.organization_id,
                    workflow_run_id=active_run_association.workflow_run_id,
                    debug_browser_session_id=active_run_association.debug_browser_session_id,
                    generation=active_run_association.generation,
                    exc_info=True,
                )
        if sensitive_run_custody_lock is not None and sensitive_run_custody_lock.locked():
            if sensitive_run_session_id is not None and not run_paused:
                release_sensitive_origin_run_lease(ctx, workflow_run_id=workflow_run.workflow_run_id)
            sensitive_run_custody_lock.release()


async def _get_run_results(
    params: dict[str, Any],
    ctx: CopilotContext,
    *,
    read_live_page: bool = True,
    admit_sensitive_origin_artifact: bool = True,
) -> dict[str, Any]:
    workflow_run_id = params.get("workflow_run_id")
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
    if WorkflowRunStatus(run.status).is_final():
        await _complete_origin_run_redaction_registry_from_runtime(ctx, workflow_run_id)
    run_browser_session_id = getattr(run, "browser_session_id", None)
    if isinstance(run_browser_session_id, str) and run_browser_session_id and WorkflowRunStatus(run.status).is_final():
        async with browser_page_custody_lock(ctx, session_id=run_browser_session_id):
            release_sensitive_origin_run_lease(ctx, workflow_run_id=workflow_run_id)

    try:
        run_workflow = await app.DATABASE.workflows.get_workflow_for_workflow_run(
            workflow_run_id,
            organization_id=ctx.organization_id,
            filter_deleted=False,
        )
    except Exception:
        run_workflow = None
    if run_workflow is None and getattr(run, "workflow_id", None):
        try:
            run_workflow = await app.DATABASE.workflows.get_workflow(
                workflow_id=run.workflow_id,
                organization_id=ctx.organization_id,
            )
        except Exception:
            run_workflow = None
    if run_workflow is None:
        LOG.warning("Prior-run workflow snapshot fetch failed", workflow_run_id=workflow_run_id)

    origin_registry = getattr(ctx, "origin_run_redaction_registry", None)
    matching_origin_registry = (
        origin_registry if origin_registry is not None and origin_registry.workflow_run_id == workflow_run_id else None
    )
    # Unknown provenance is not evidence that a cold run was nonsensitive. Use the same
    # fail-closed classification for every direct page producer and for terminal artifacts.
    workflow_has_sensitive_parameters = _workflow_requires_terminal_artifact_redaction(run_workflow)
    sensitive_origin_run = (
        _origin_registry_contains_sensitive_values(matching_origin_registry, workflow_run_id)
        or workflow_has_sensitive_parameters
    )

    blocks = await app.DATABASE.observer.get_workflow_run_blocks(
        workflow_run_id=workflow_run_id,
        organization_id=ctx.organization_id,
    )

    results = []
    for block in blocks:
        block_result = _recorded_run_block_result(block)
        results.append(block_result)

    await _attach_action_traces(blocks, results, ctx.organization_id)
    if not sensitive_origin_run:
        await _attach_failed_block_screenshots(blocks, results, ctx.organization_id)

    first_failed = _first_failed_result(results)
    action_trace_summary = _failure_action_trace_summary(first_failed)
    action_observations = _retained_action_observations(results)
    result_data: dict[str, Any] = {
        "workflow_run_id": workflow_run_id,
        "browser_session_id": run.browser_session_id,
        "overall_status": run.status,
        "requested_block_labels": [result["label"] for result in results if result.get("label")],
        "executed_block_labels": [
            result["label"]
            for result in results
            if result.get("label") and result.get("status") in _EXECUTED_BLOCK_STATUSES
        ],
        "blocks": results,
        "failing_code_line": _failing_code_line(first_failed.get("action_trace")) if first_failed else None,
        "action_trace_summary": action_trace_summary,
        "action_observations": action_observations,
    }
    # When worker-dispatch is enabled for this copilot session the run's persistent browser
    # session is worker-owned (for a non-fresh run ctx.browser_session_id == run_session_id), so
    # the API must not attach to it over CDP. Mirror the gating in _run_blocks_and_collect_debug.
    dispatch_to_worker = await app.AGENT_FUNCTION.should_dispatch_copilot_block_run_to_worker(
        organization_id=ctx.organization_id,
        workflow_permanent_id=ctx.workflow_permanent_id,
    )
    locator_observations: list[AuthoredLocatorObservationRow] | None = None
    if not sensitive_origin_run:
        failed_block_code = _failed_block_code(run_workflow, first_failed) if run_workflow is not None else None
        locator_observations = await _observe_authored_locators(
            ctx,
            run_session_id=run.browser_session_id,
            failed_block_code=failed_block_code,
            worker_owned=dispatch_to_worker,
            observation_deadline_exceeded=getattr(ctx, "copilot_total_timeout_exceeded", False),
        )
    if locator_observations is not None:
        result_data["authored_locator_observations"] = locator_observations
    dispatched_end_url = (
        _dispatched_end_url(list(reversed(blocks))) if dispatch_to_worker and not sensitive_origin_run else None
    )
    if sensitive_origin_run:
        current_url, page_title = "", ""
    elif dispatch_to_worker:
        current_url, page_title = dispatched_end_url or "", ""
    elif read_live_page:
        current_url, page_title = await _fallback_page_info(ctx)
    else:
        # Outside a call's dynamic extent this would resolve the chat's own browser and report its
        # page as the finished run's, which is the substitution the repair binding refuses to make.
        current_url, page_title = "", ""
    if current_url:
        result_data["current_url"] = current_url
        if not dispatch_to_worker:
            result_data["current_url_live_observed"] = True
        if page_title:
            result_data["page_title"] = page_title
    if dispatch_to_worker and dispatched_end_url is None:
        result_data["current_url_evidence"] = NO_PERSISTED_END_URL
    if getattr(run, "failure_reason", None):
        result_data["failure_reason"] = run.failure_reason
    await _halt_turn_if_superseded(
        ctx,
        run,
        workflow_run_id=workflow_run_id,
    )

    if run_workflow is None:
        result_data["requested_output_definitions_omission"] = "the run-pinned workflow snapshot was unavailable"
    else:
        result_data["requested_output_parameter_definitions"] = _requested_output_parameter_definitions(
            workflow_run_id=workflow_run_id,
            workflow=run_workflow,
        )
    await _attach_registered_output_parameter_values(
        workflow_run_id=workflow_run_id,
        workflow=run_workflow,
        data=result_data,
        persisted_output_parameters=(
            [parameter for parameter in _workflow_parameters(run_workflow) if isinstance(parameter, OutputParameter)]
            if run_workflow is not None
            else None
        ),
    )

    cold_artifact_requires_redaction_context = _workflow_requires_terminal_artifact_redaction(run_workflow)
    # Cold ordinary-turn hydration did not perform a sensitive run and cannot use mutable context
    # alone to vouch for its raw artifact custody. The direct same-turn tool route may use its exact
    # immutable registry; cold sensitive hydration receives a typed omission instead.
    artifact_redaction_registry = (
        matching_origin_registry if sensitive_origin_run and admit_sensitive_origin_artifact else None
    )
    artifact_has_redaction_context = (
        artifact_redaction_registry is not None and artifact_redaction_registry.contains_all_sensitive_values
    )
    terminal_page_evidence = (
        None
        if (cold_artifact_requires_redaction_context or sensitive_origin_run) and not artifact_has_redaction_context
        else await _fetch_dispatched_terminal_page_evidence(
            run_id=workflow_run_id,
            organization_id=ctx.organization_id,
            current_url=current_url,
            workflow=run_workflow,
            origin_redaction_registry=artifact_redaction_registry,
        )
    )
    if terminal_page_evidence is None:
        result_data["terminal_page_evidence_omission"] = (
            "persisted terminal page artifact was not admitted without origin-run credential redaction context"
            if cold_artifact_requires_redaction_context or sensitive_origin_run
            else "no persisted terminal page artifact was available"
        )
    else:
        stamped_page_evidence = stamp_page_evidence_provenance(
            terminal_page_evidence,
            source_browser_session_id=run.browser_session_id,
            run_id=workflow_run_id,
            run_browser_session_id=run.browser_session_id,
        )
        result_data["post_run_page_evidence"] = model_visible_composition_evidence(stamped_page_evidence)

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
    if composition_challenge_carrier(evidence) is None:
        LOG.info(
            "copilot anti-bot composition evidence keyword-only-suppressed",
            indicator_count=len(normalized_indicators),
            challenge_control_count=control_count,
            challenge_detected=challenge_detected,
        )
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


@dataclass(frozen=True)
class TerminalChallengeEvidence:
    source: str
    reason: str
    challenge_evidence_source: str
    workflow_run_id: str | None = None
    block_labels: tuple[str, ...] = ()
    challenge_kind: ChallengeKind | None = None


def _trusted_terminal_challenge_category_names(failure_categories: list[dict] | None) -> tuple[str, ...]:
    if not isinstance(failure_categories, list):
        return ()
    names: list[str] = []
    for category in failure_categories:
        if not isinstance(category, dict):
            continue
        name = trusted_terminal_challenge_category_name(category)
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(dict.fromkeys(names))


def _ensure_terminal_challenge_category(data: dict[str, Any], *, challenge_evidence_source: str) -> None:
    categories = data.get("failure_categories")
    if not isinstance(categories, list):
        categories = []
    if not any(
        isinstance(category, dict) and trusted_terminal_challenge_category_name(category) for category in categories
    ):
        categories = [
            *categories,
            {
                "category": "ANTI_BOT_DETECTION",
                "confidence_float": 0.9,
                "reasoning": "Structured challenge evidence reported a terminal blocker.",
                "evidence_source": challenge_evidence_source,
            },
        ]
    data["failure_categories"] = categories


def _block_labels_from_result_data(data: Mapping[str, object]) -> tuple[str, ...]:
    blocks = data.get("blocks")
    if not isinstance(blocks, list):
        return ()
    labels: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        label = block.get("label")
        if isinstance(label, str) and label.strip():
            labels.append(label.strip())
    return tuple(dict.fromkeys(labels))


def _apply_terminal_challenge_latches(
    copilot_ctx: CopilotContext, result: dict[str, Any], terminal_challenge: TerminalChallengeEvidence
) -> None:
    result["ok"] = False
    result.setdefault("error", terminal_challenge.reason)
    data = result.get("data")
    if isinstance(data, dict):
        data.setdefault("failure_reason", terminal_challenge.reason)
        _ensure_terminal_challenge_category(
            data, challenge_evidence_source=terminal_challenge.challenge_evidence_source
        )
        copilot_ctx.last_failure_category_top = "ANTI_BOT_DETECTION"
    LOG.info(
        "copilot anti-bot evidence stamp",
        anti_bot_evidence_source=terminal_challenge.challenge_evidence_source,
        stamp_seam="terminal_challenge",
    )
    copilot_ctx.last_test_ok = False
    copilot_ctx.last_test_suspicious_success = False
    copilot_ctx.last_test_failure_reason = terminal_challenge.reason
    copilot_ctx.last_test_anti_bot = terminal_challenge.reason
    copilot_ctx.last_full_workflow_test_ok = False
    copilot_ctx.last_failed_workflow_yaml = copilot_ctx.workflow_yaml
    # A challenge used to fire before the success branch could run, so these were unreachable for a
    # challenged run; the grade now precedes the settle and can set them.
    copilot_ctx.verified_terminal_proposal_ready = False


def settle_terminal_challenge_after_enrichment(copilot_ctx: CopilotContext, result: dict[str, Any]) -> bool:
    """Re-decide the terminal challenge once post-run page evidence exists, since the grade committed
    at handback could not see it. It may only turn a reported pass into a failure, never the reverse."""
    if result.get("ok") is not True:
        return False
    structured_blocker = _run_blocks_structured_blocker_message(result, copilot_ctx)
    anti_bot_match, _, failure_categories, _ = _analyze_run_blocks(result, copilot_ctx)
    anti_bot_source = first_carrier_backed_anti_bot_source(failure_categories) if anti_bot_match else None
    anti_bot_evidence_source = anti_bot_source.value if anti_bot_source else None
    if not anti_bot_match:
        anti_bot_match = _composition_anti_bot_reason(copilot_ctx)
        if anti_bot_match:
            composition_source = composition_challenge_carrier(copilot_ctx.composition_page_evidence)
            anti_bot_evidence_source = composition_source.value if composition_source else None
    if anti_bot_match and not anti_bot_evidence_source:
        # Keyword-only, same as the pre-enrichment path: drop the match but keep going, because a
        # structured blocker plus a challenge artifact still decides this without it.
        LOG.info("copilot anti-bot latch keyword-only-suppressed")
        anti_bot_match = None
    terminal_challenge = _terminal_challenge_evidence(
        result,
        failure_categories=failure_categories,
        structured_blocker=structured_blocker,
        anti_bot_match=anti_bot_match,
        anti_bot_evidence_source=anti_bot_evidence_source,
        artifact_flag_key=_artifact_challenge_flag_from_result(result, copilot_ctx),
        challenge_kind=_terminal_challenge_kind(copilot_ctx, result),
    )
    if terminal_challenge is None:
        return False
    _apply_terminal_challenge_latches(copilot_ctx, result, terminal_challenge)
    # The committed outcome still reads as the pass this run looked like; the emitted envelope would
    # otherwise contradict the failure the caller now returns.
    data = result.get("data")
    run_id = data.get("workflow_run_id") if isinstance(data, dict) else None
    _stash_recorded_run_outcome(
        copilot_ctx,
        RecordedRunOutcome(
            verdict="not_demonstrated",
            reason_code="blocker_reported",
            display_reason=run_outcome_display_reason(terminal_challenge.reason),
            workflow_run_id=run_id if isinstance(run_id, str) else None,
            run_completed=False,
        ),
    )
    return True


def _terminal_challenge_evidence(
    result: dict[str, Any],
    *,
    failure_categories: list[dict] | None,
    structured_blocker: str | None,
    anti_bot_match: str | None = None,
    anti_bot_evidence_source: str | None = None,
    artifact_flag_key: str | None = None,
    challenge_kind: ChallengeKind | None = None,
) -> TerminalChallengeEvidence | None:
    data = result.get("data")
    result_data = data if isinstance(data, dict) else {}
    workflow_run_id = result_data.get("workflow_run_id")
    run_id = workflow_run_id if isinstance(workflow_run_id, str) and workflow_run_id.strip() else None
    block_labels = _block_labels_from_result_data(result_data)
    challenge_categories = _trusted_terminal_challenge_category_names(failure_categories)
    corroborated_match = anti_bot_match if isinstance(anti_bot_evidence_source, str) else None
    if isinstance(structured_blocker, str) and (
        isinstance(artifact_flag_key, str) or isinstance(corroborated_match, str)
    ):
        # Prefer the typed blocker payload over category fallback when both are
        # present because it carries the concrete page/run blocker text.
        reason = f"Run output reported a blocker: {structured_blocker}"
        if (
            isinstance(corroborated_match, str)
            and corroborated_match.strip()
            and not _looks_like_anti_bot_blocker(structured_blocker)
        ):
            reason = f"{corroborated_match}; {reason}"
        return TerminalChallengeEvidence(
            source="structured_blocker",
            reason=reason,
            challenge_evidence_source=(
                ChallengeEvidenceSource.ARTIFACT.value if artifact_flag_key else str(anti_bot_evidence_source)
            ),
            workflow_run_id=run_id,
            block_labels=block_labels,
            challenge_kind=challenge_kind,
        )
    if challenge_categories:
        reason = next(iter_failure_reasons(result), None) or f"Run reported {challenge_categories[0]}"
        category_source = first_carrier_backed_anti_bot_source(failure_categories)
        return TerminalChallengeEvidence(
            source="failure_category",
            reason=reason,
            challenge_evidence_source=(
                category_source.value if category_source else ChallengeEvidenceSource.CHALLENGE_STATE.value
            ),
            workflow_run_id=run_id,
            block_labels=block_labels,
            challenge_kind=challenge_kind,
        )
    return None


def _terminal_challenge_completion_verification(
    completion_verification: CompletionVerificationResult | None, reason: str
) -> CompletionVerificationResult | None:
    if completion_verification is None or completion_verification.status != "evaluated":
        return completion_verification
    criterion_ids = list(completion_verification.criterion_ids)
    if not criterion_ids:
        return completion_verification
    return replace(
        completion_verification,
        verdicts=[
            CriterionVerdict(
                criterion_id=criterion_id,
                state="unsatisfied",
                reason_code=TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE,
                missing_evidence=reason,
            )
            for criterion_id in criterion_ids
        ],
    )


# Generic failure-reason template emitted by the shared agent when the
# browser-side scraper catches ScrapingFailed / NoElementFound. Matching on
# the template (not the shared classifier) lets the copilot notice a repeated
# site-block/unreadable-page pattern even though the classifier routes it to
# DATA_EXTRACTION_FAILURE, not ANTI_BOT_DETECTION.
# Coupling note: these substrings come from the run-level failure_reason
# produced when the shared scraper raises ScrapingFailed; update the tuple if that wording changes.
def _detect_non_retriable_nav_error(result: dict[str, Any]) -> str | None:
    """Return the first failure_reason that matches SKIP_INNER_NAV_RETRY_ERRORS
    (DNS / cert / SSL / invalid URL), preferring run-level over block-level.
    Same set is_skip_inner_retry_error uses at the browser layer, so the copilot
    classifies on exactly the patterns that already short-circuit retries in
    navigate_with_retry (skyvern/webeye/navigation.py)."""
    return next((reason for reason in iter_failure_reasons(result) if is_skip_inner_retry_error(reason)), None)


def _infrastructure_runner_error_codes(result: dict[str, Any]) -> list[str]:
    data = result.get("data")
    blocks = data.get("blocks") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        return []
    found: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        codes = block.get("error_codes")
        if not isinstance(codes, list):
            continue
        for code in codes:
            if isinstance(code, str) and code in INFRASTRUCTURE_RUNNER_ERROR_CODES and code not in found:
                found.append(code)
    return found


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
    current_url = _valid_runtime_anchor_url(data.get("current_url"))
    if current_url is not None:
        evidence.current_url = current_url
        # Only a live read of the page verifies its state. A writer that omits the marker — a
        # dispatched run reading worker-persisted rows, or a new caller — must not claim otherwise,
        # so the absent case is the unverified one. The flag is only ever set True and survives an
        # earlier live read, so it has to move with the URL it describes: leaving it set beside a
        # worker-persisted URL would report a verification of a page this one is not.
        evidence.live_page_state_verified = data.get("current_url_live_observed") is True
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
    failure_reason = copilot_ctx.last_test_failure_reason or result.get("error")
    if isinstance(failure_reason, str) and failure_reason.strip():
        evidence.failure_reason = " ".join(failure_reason.split())[:240]


def _read_mapping_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not part or not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _record_run_blocks_result(
    copilot_ctx: Any,
    result: dict[str, Any],
    completion_verification: CompletionVerificationResult | None = None,
    connect_failure_reason: str | None = None,
) -> RecordedRunOutcome | None:
    """Record the run result on ctx without letting a second judge rewrite it."""
    _record_executed_block_labels(copilot_ctx, result)
    run_ok = bool(result.get("ok", False))
    data = result.get("data")
    run_id = data.get("workflow_run_id") if isinstance(data, dict) else None
    if isinstance(run_id, str) and run_id:
        copilot_ctx.block_run_calls_this_turn = _runs_this_turn(copilot_ctx) + 1
        copilot_ctx.dispatched_run_ids_this_turn.add(run_id)
    # ADR-0025: interactive authoring has no post-run adjudicator. Keep the
    # argument temporarily for callers/tests while making it deliberately inert;
    # the unattended page-observation self-heal verifier remains a separate lane.
    copilot_ctx.completion_verification_result = None
    copilot_ctx.last_run_blocks_workflow_run_id = run_id if isinstance(run_id, str) else None
    run_browser_session_id = data.get("browser_session_id") if isinstance(data, dict) else None
    copilot_ctx.last_run_blocks_browser_session_id = (
        run_browser_session_id if isinstance(run_browser_session_id, str) and run_browser_session_id else None
    )
    copilot_ctx.last_successful_run_blocks_workflow_run_id = run_id if run_ok and isinstance(run_id, str) else None
    # Watchdog cancels normally count as ok=False; only a coincident total
    # timeout softens to ``None`` to keep the unvalidated WIP rescue open.
    cancelled_by_watchdog = result.get(_INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY) is True
    timeout_latched = bool(copilot_ctx.copilot_total_timeout_exceeded)
    # A pause softens the same way: left at False the generic failed-test nudge rewrites the reply
    # into "the test failed" about a run that is alive and waiting, and ``None`` still bars a
    # verified proposal because that gate requires ``is True``.
    run_paused = isinstance(data, dict) and (data.get("control_signal") or {}).get("kind") == "watchdog_paused"
    copilot_ctx.last_test_ok = None if run_paused or (cancelled_by_watchdog and timeout_latched) else run_ok
    copilot_ctx.last_full_workflow_test_ok = False
    # Re-affirmed per run below only when this run satisfies completion; never let a
    # prior run's terminal-ready latch leak into a run that did not verify.
    copilot_ctx.verified_terminal_proposal_ready = False
    copilot_ctx.last_unverified_block_labels = _unverified_current_workflow_labels(copilot_ctx)
    copilot_ctx.last_test_failure_reason = None
    copilot_ctx.last_artifact_health_blocker_reason = None
    copilot_ctx.last_artifact_health_blocker_labels = []
    copilot_ctx.last_artifact_health_failure_classes = []
    copilot_ctx.last_test_suspicious_success = False
    copilot_ctx.last_run_outcome = None
    copilot_ctx.last_run_outcome_block_labels = []
    copilot_ctx.last_test_anti_bot = None
    copilot_ctx.last_failure_category_top = None
    copilot_ctx.last_test_non_retriable_nav_error = None
    copilot_ctx.last_infrastructure_tool_error = None
    copilot_ctx.post_run_page_observation_tool = None
    copilot_ctx.post_run_page_observation_url = None
    copilot_ctx.post_run_page_observation_workflow_run_id = None
    copilot_ctx.post_run_page_observation_after_failed_test = False
    copilot_ctx.post_run_current_page_inspection_workflow_run_id = None
    record_pending_runtime_authoring_repair_context(copilot_ctx, result)

    structured_blocker = _run_blocks_structured_blocker_message(result, copilot_ctx)
    anti_bot_match, empty_data_blocks, failure_categories, goal_path_omissions = _analyze_run_blocks(
        result, copilot_ctx
    )
    infrastructure_runner_codes = _infrastructure_runner_error_codes(result)
    if infrastructure_runner_codes:
        copilot_ctx.last_infrastructure_tool_error = ", ".join(infrastructure_runner_codes)
        # Prepended, not appended: `last_failure_category_top` reads entry zero, and the
        # infrastructure fault outranks whatever the block's prose was classified as.
        failure_categories = [
            {
                "category": "UNRECOVERABLE_TOOL_ERROR",
                "confidence_float": 1.0,
                "reasoning": (
                    "The code sandbox was unreachable "
                    f"({copilot_ctx.last_infrastructure_tool_error}); no edit to the block can reach it."
                ),
            },
            *(failure_categories or []),
        ]
    artifact_flag_key = _artifact_challenge_flag_from_result(result, copilot_ctx)
    anti_bot_source = first_carrier_backed_anti_bot_source(failure_categories) if anti_bot_match else None
    anti_bot_evidence_source = anti_bot_source.value if anti_bot_source else None
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
            composition_source = composition_challenge_carrier(copilot_ctx.composition_page_evidence)
            anti_bot_evidence_source = composition_source.value if composition_source else None
    if anti_bot_match and anti_bot_evidence_source:
        copilot_ctx.last_test_anti_bot = anti_bot_match
        LOG.info(
            "copilot anti-bot evidence stamp",
            anti_bot_evidence_source=anti_bot_evidence_source,
            stamp_seam="last_test_anti_bot",
        )
    elif anti_bot_match:
        LOG.info("copilot anti-bot latch keyword-only-suppressed")
        anti_bot_match = None
    if failure_categories:
        carrier_categories = carrier_backed_anti_bot_categories(failure_categories)
        top = carrier_categories[0] if carrier_categories else None
        if isinstance(top, dict):
            top_category = top.get("category")
            if isinstance(top_category, str):
                copilot_ctx.last_failure_category_top = top_category

    # Expose full failure classification in tool output for agent reasoning
    if failure_categories:
        data = result.get("data")
        if isinstance(data, dict):
            data["failure_categories"] = failure_categories

    terminal_challenge = _terminal_challenge_evidence(
        result,
        failure_categories=failure_categories,
        structured_blocker=structured_blocker,
        anti_bot_match=anti_bot_match,
        anti_bot_evidence_source=anti_bot_evidence_source,
        artifact_flag_key=artifact_flag_key,
        challenge_kind=_terminal_challenge_kind(copilot_ctx, result),
    )

    artifact_reason, artifact_labels, artifact_classes = _artifact_health_blocker_from_result(result)
    if artifact_reason is not None:
        copilot_ctx.last_artifact_health_blocker_reason = artifact_reason
        copilot_ctx.last_artifact_health_blocker_labels = artifact_labels
        copilot_ctx.last_artifact_health_failure_classes = artifact_classes
        data = result.get("data")
        if isinstance(data, dict):
            data["artifact_health_blocker"] = {
                "reason": artifact_reason,
                "failed_block_labels": artifact_labels,
                "failure_classes": artifact_classes,
            }

    if terminal_challenge is not None:
        _apply_terminal_challenge_latches(copilot_ctx, result, terminal_challenge)
        run_ok = False

    if run_ok:
        output_report = recorded_output_report(
            data.get("registered_output_parameter_values") if isinstance(data, dict) else None
        )
        recorded_outcome = _recorded_run_outcome(
            workflow_run_id=run_id if isinstance(run_id, str) else None,
            output_report=output_report,
            run_completed=run_ok,
        )
        unverified = _unverified_current_workflow_labels(copilot_ctx)
        composition_unverified = _composition_unverified_current_workflow_labels(copilot_ctx)
        result_blocks = data.get("blocks") if isinstance(data, dict) else None
        executed_labels = data.get("executed_block_labels") if isinstance(data, dict) else None
        has_executed_blocks = bool(
            getattr(copilot_ctx, "last_run_blocks_block_labels", None)
            or (executed_labels if isinstance(executed_labels, list) else None)
            or (result_blocks if isinstance(result_blocks, list) else None)
        )
        copilot_ctx.last_unverified_block_labels = unverified
        copilot_ctx.last_failed_workflow_yaml = None
        copilot_ctx.last_test_failure_reason = None
        copilot_ctx.last_test_suspicious_success = False
        terminal_ready = terminal_ready_for_latch(
            current_workflow_labels=_current_workflow_block_labels(copilot_ctx),
            has_executed_blocks=has_executed_blocks,
            unverified=unverified,
            composition_unverified=composition_unverified,
            artifact_reason=artifact_reason,
            structured_blocker=structured_blocker,
            empty_data_blocks=empty_data_blocks,
        )
        copilot_ctx.verified_terminal_proposal_ready = terminal_ready
        if frontier_dump_root() is not None:
            write_packet(
                "latch",
                {
                    "trust": trust_snapshot(copilot_ctx),
                    "current_workflow_labels": _current_workflow_block_labels(copilot_ctx),
                    "unverified": unverified,
                    "composition_unverified": composition_unverified,
                    "has_executed_blocks": has_executed_blocks,
                    "artifact_reason": artifact_reason,
                    "structured_blocker": structured_blocker,
                    "empty_data_blocks": empty_data_blocks,
                    "terminal_ready": terminal_ready,
                },
            )
        copilot_ctx.last_full_workflow_test_ok = terminal_ready
        if copilot_ctx.last_full_workflow_test_ok:
            copilot_ctx.last_unverified_block_labels = []
            copilot_ctx.last_good_workflow = copilot_ctx.last_workflow
            copilot_ctx.last_good_workflow_yaml = copilot_ctx.last_workflow_yaml
        _update_verification_evidence_from_run_result(copilot_ctx, result)
        _record_build_test_outcome(copilot_ctx, result, recorded_outcome, goal_path_omissions)
        return _stash_recorded_run_outcome(copilot_ctx, recorded_outcome)

    copilot_ctx.last_failed_workflow_yaml = getattr(copilot_ctx, "workflow_yaml", None)
    copilot_ctx.last_test_non_retriable_nav_error = _detect_non_retriable_nav_error(result)

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
    if connect_failure_reason:
        # User-facing surfaces read this; the result dict keeps the run's own reason for the
        # repair signature, the failure summary and loop detection.
        copilot_ctx.last_test_failure_reason = connect_failure_reason
    _update_verification_evidence_from_run_result(copilot_ctx, result)
    recorded_outcome = RecordedRunOutcome(
        verdict="not_demonstrated",
        reason_code="blocker_reported",
        display_reason=run_outcome_display_reason(
            connect_failure_reason
            or copilot_ctx.last_test_failure_reason
            or str(result.get("error") or "The run failed.")
        ),
        workflow_run_id=run_id if isinstance(run_id, str) else None,
        run_completed=False,
    )
    _record_build_test_outcome(copilot_ctx, result, recorded_outcome, goal_path_omissions)
    return _stash_recorded_run_outcome(copilot_ctx, recorded_outcome)


_EXECUTED_BLOCK_STATUSES = frozenset(status.value for status in BlockStatus if status != BlockStatus.skipped)


def _commit_run_blocks_record(copilot_ctx: CopilotContext, result: dict[str, Any]) -> RecordedRunOutcome | None:
    """Commit the run's structured outcome once, marking the result so the shared seam does not redo it.

    The browser-loss stamp runs here rather than at the shared seam: the commit happens upstream of
    that seam, so a stamp applied there would never reach the committed record."""
    connect_failure_reason = _stamp_run_side_connect_failure(copilot_ctx, result)
    recorded = _record_run_blocks_result(
        copilot_ctx, result, completion_verification=None, connect_failure_reason=connect_failure_reason
    )
    result[_INTERNAL_RUN_OUTCOME_RECORDED_KEY] = True
    return recorded


def _record_executed_block_labels(copilot_ctx: CopilotContext, result: dict[str, Any]) -> None:
    data = result.get("data")
    blocks = data.get("blocks") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        return
    fingerprints = workflow_block_fingerprints(copilot_ctx.workflow_yaml or "")
    for block in blocks:
        if not isinstance(block, dict) or block.get("status") not in _EXECUTED_BLOCK_STATUSES:
            continue
        label = block.get("label")
        if isinstance(label, str) and label:
            copilot_ctx.executed_block_labels.add(label)
            block_fingerprints = fingerprints.get(label)
            if block_fingerprints is not None:
                copilot_ctx.executed_block_fingerprints.setdefault(label, set()).update(block_fingerprints)


def _build_recorded_build_test_outcome(
    copilot_ctx: CopilotContext,
    result: dict[str, Any],
    recorded_run_outcome: RecordedRunOutcome | None,
    declared_goal_path_omissions: Sequence[Mapping[str, str]] | None = None,
) -> RecordedBuildTestOutcome | None:
    raw_result_data = result.get("data")
    result_data = raw_result_data if isinstance(raw_result_data, dict) else {}
    requested_output_parameter_payloads: list[BuildTestPacketRequestedOutput] | None = (
        _packet_requested_outputs(
            result_data,
            _packet_string(result_data.get("workflow_run_id")),
            [],
        )
        or None
    )
    registered_output_parameter_payloads = (
        result_data.get("registered_output_parameter_values")
        if isinstance(result_data.get("registered_output_parameter_values"), list)
        else None
    )
    workflow_yaml = copilot_ctx.workflow_yaml
    code_artifact_metadata = copilot_ctx.code_artifact_metadata
    workflow_definition = getattr(copilot_ctx.last_workflow, "workflow_definition", None)
    raw_requested_block_labels = getattr(copilot_ctx, "last_requested_block_labels", None)
    result_requested_block_labels = result_data.get("requested_block_labels")
    raw_requested_values: list[Any] = (
        raw_requested_block_labels
        if isinstance(raw_requested_block_labels, list)
        else (result_requested_block_labels if isinstance(result_requested_block_labels, list) else [])
    )
    requested_block_labels = [label for label in raw_requested_values if isinstance(label, str)]
    block_shape_hashes = block_shape_hashes_by_label(
        requested_block_labels,
        workflow_definition,
    )
    result_page_evidence = (
        result_data.get("post_run_page_evidence")
        if isinstance(result_data.get("post_run_page_evidence"), dict)
        else None
    )
    return recorded_outcome_from_run_blocks_result(
        result,
        page_evidence=result_page_evidence or copilot_ctx.composition_page_evidence,
        recorded_run_outcome=recorded_run_outcome,
        completion_verification=None,
        authored_structure_signature=authored_structure_signature_from_workflow(
            workflow_yaml,
            code_artifact_metadata,
        ),
        requested_output_parameter_payloads=requested_output_parameter_payloads,
        registered_output_parameter_payloads=registered_output_parameter_payloads,
        declared_goal_path_omissions=declared_goal_path_omissions,
        unbound_required_parameter_keys=list(copilot_ctx.unbound_required_parameter_keys),
        block_parameter_keys=authored_block_parameter_keys_from_workflow(
            workflow_yaml,
            code_artifact_metadata,
        ),
        block_shape_hashes=block_shape_hashes,
        block_associations_by_label=getattr(copilot_ctx, "runner_code_block_associations_by_label", {}),
    )


def _record_build_test_outcome(
    copilot_ctx: CopilotContext,
    result: dict[str, Any],
    recorded_run_outcome: RecordedRunOutcome | None,
    declared_goal_path_omissions: Sequence[Mapping[str, str]] | None = None,
) -> None:
    # Kept on the result so a post-enrichment regrade reads the same omission facts this grade did.
    result[_INTERNAL_GOAL_PATH_OMISSIONS_KEY] = list(declared_goal_path_omissions or [])
    record_build_test_outcome(
        copilot_ctx,
        _build_recorded_build_test_outcome(
            copilot_ctx,
            result,
            recorded_run_outcome,
            declared_goal_path_omissions,
        ),
    )


def _stash_recorded_run_outcome(copilot_ctx: Any, outcome: RecordedRunOutcome) -> RecordedRunOutcome:
    if outcome.workflow_run_id is None:
        outcome = replace(outcome, workflow_run_id=getattr(copilot_ctx, "last_run_blocks_workflow_run_id", None))
    copilot_ctx.last_run_outcome = outcome
    copilot_ctx.last_run_outcome_block_labels = list(getattr(copilot_ctx, "last_run_blocks_block_labels", []) or [])
    return outcome


def _recorded_run_outcome(
    *,
    workflow_run_id: str | None = None,
    output_report: str | None = None,
    run_completed: bool | None = None,
) -> RecordedRunOutcome:
    """Record the completed run status without interpreting whether it met the request."""
    return RecordedRunOutcome(
        verdict="not_evaluated",
        workflow_run_id=workflow_run_id,
        output_report=output_report,
        run_completed=run_completed,
    )


async def _send_run_started_update(copilot_ctx: CopilotContext, workflow_run_id: str) -> None:
    try:
        await copilot_ctx.stream.send(
            WorkflowCopilotRunStartedUpdate(
                type=WorkflowCopilotStreamMessageType.RUN_STARTED,
                workflow_run_id=workflow_run_id,
                timestamp=datetime.now(UTC),
            )
        )
    except Exception:
        LOG.debug("copilot run_started send failed", exc_info=True)


async def _send_run_outcome_update(
    copilot_ctx: Any,
    result: dict[str, Any],
    *,
    verdict: RunOutcomeVerdict,
    reason_code: RunOutcomeReasonCode | None,
    display_reason: str | None,
    role: RunOutcomeRole = "recorded",
) -> None:
    stream = getattr(copilot_ctx, "stream", None)
    if stream is None:
        return
    data = result.get("data")
    run_id = data.get("workflow_run_id") if isinstance(data, dict) else None
    browser_session_id = data.get("browser_session_id") if isinstance(data, dict) else None
    overall_status = data.get("overall_status") if isinstance(data, dict) else None
    control_signal = data.get("control_signal") if isinstance(data, dict) else None
    control_kind = control_signal.get("kind") if isinstance(control_signal, dict) else None
    terminal_disposition = control_kind if isinstance(control_kind, str) else overall_status
    narrator_state = getattr(copilot_ctx, "narrator_state", None)
    iteration = narrator_state.current_iteration if narrator_state is not None else 0
    try:
        await stream.send(
            WorkflowCopilotRunOutcomeUpdate(
                type=WorkflowCopilotStreamMessageType.RUN_OUTCOME,
                workflow_run_id=run_id if isinstance(run_id, str) else "",
                workflow_run_block_ids=list(getattr(copilot_ctx, "last_run_blocks_block_ids", []) or []),
                block_labels=list(getattr(copilot_ctx, "last_run_blocks_block_labels", []) or []),
                verdict=verdict,
                role=role,
                reason_code=reason_code,
                display_reason=display_reason,
                browser_session_id=browser_session_id if isinstance(browser_session_id, str) else None,
                workflow_permanent_id=copilot_ctx.workflow_permanent_id,
                turn_id=copilot_ctx.turn_id,
                workflow_copilot_chat_id=copilot_ctx.workflow_copilot_chat_id,
                continuity_source="workflow_run",
                terminal_disposition=terminal_disposition if isinstance(terminal_disposition, str) else None,
                iteration=iteration,
                timestamp=datetime.now(UTC),
            )
        )
    except Exception:
        LOG.debug("copilot run_outcome send failed", exc_info=True)


def _mark_stored_post_run_failure_page(copilot_ctx: Any) -> None:
    run_id = copilot_ctx.last_run_blocks_workflow_run_id
    evidence = copilot_ctx.composition_page_evidence
    if not post_run_inspection_cleanly_matches(evidence, run_id):
        return
    url = evidence.get("current_url") or evidence.get("inspected_url") or ""
    _mark_post_run_page_observed(
        copilot_ctx,
        source_tool="inspect_page_for_composition",
        url=url,
        page_evidence=evidence,
        source_browser_session_id=evidence.get("source_browser_session_id"),
    )
    page_title = evidence.get("page_title")
    if isinstance(page_title, str) and page_title:
        _workflow_verification_evidence(copilot_ctx).page_title = page_title[:160]


async def _resolve_captcha_solver_availability(copilot_ctx: Any) -> None:
    """Answer once, where we are async, whether this deployment can clear a captcha on this page.

    The sync diagnosis and repair readers cannot await, and an unanswered question reads as no
    solver, so resolving it here is what keeps a clearable captcha from being treated as a wall.
    """
    evidence = getattr(copilot_ctx, "composition_page_evidence", None)
    url = None
    if isinstance(evidence, dict):
        url = evidence.get("current_url") or evidence.get("inspected_url")
    try:
        available = (
            await app.AGENT_FUNCTION.captcha_solving_available(getattr(copilot_ctx, "organization_id", None), url)
            is True
        )
    except Exception:
        LOG.warning("copilot captcha solver availability lookup failed; treating as unavailable", exc_info=True)
        available = False
    copilot_ctx.captcha_solver_available = available
    copilot_ctx.captcha_solver_available_for_url = url


def _carry_unresolved_failure_into_result(copilot_ctx: Any, result: dict[str, Any], tool_name: str = "") -> None:
    """Keep an earlier unresolved failure visible in the result this run returns to the model.

    A later success otherwise displaces the failure before the model decides whether to repair: it
    sees a passing run, has no record of the one that failed, and reports done. The fact is reported,
    not interpreted - which run failed, at which block, how, and that this success did not establish
    the failure was resolved. The model still owns what to do about it.
    """
    data = result.get("data")
    if not result.get("ok"):
        _carry_prior_attempt_change_identity_into_result(copilot_ctx, data, tool_name)
        return
    this_run_id = data.get("workflow_run_id") if isinstance(data, dict) else None
    unresolved, disposition = unresolved_runtime_block_failure_with_disposition(
        copilot_ctx,
        reported_workflow_yaml=copilot_ctx.persisted_workflow_yaml,
        pending_later_run_id=this_run_id if isinstance(this_run_id, str) else None,
        reported_workflow_is_persisted=True,
    )
    # This seam is load-bearing for repair: a failure it fails to carry is one the model decides
    # without. One bounded event per successful hand-back, so a miss is attributable rather than
    # silent.
    LOG.info(
        "copilot run result unresolved-failure disposition",
        tool_name=tool_name,
        workflow_run_id=this_run_id,
        retained_history=len(getattr(copilot_ctx, "recorded_build_test_outcome_history", [])),
        disposition=disposition,
        attached=unresolved is not None,
    )
    if unresolved is None:
        return
    failure_kind = ""
    for entry in reversed(getattr(copilot_ctx, "recorded_build_test_outcome_history", [])):
        if entry.get("workflow_run_id") == unresolved.workflow_run_id:
            failure_kind = _safe_reason_code(entry.get("reason_code"))
            break
    if not isinstance(data, dict):
        return
    data["unresolved_earlier_failure"] = {
        "workflow_run_id": unresolved.workflow_run_id,
        "block_label": unresolved.block_label,
        "failure_kind": failure_kind,
        "note": "this run passing does not establish that the earlier failure was resolved",
    }
    LOG.info(
        "copilot carried unresolved failure into run result",
        workflow_run_id=unresolved.workflow_run_id,
        block_label=unresolved.block_label,
        failure_kind=failure_kind,
    )


def _carry_prior_attempt_change_identity_into_result(
    copilot_ctx: CopilotContext, data: dict[str, Any] | None, tool_name: str
) -> None:
    """Tell a repeat failing hand-back whether the code it just ran differs from the code that raised.
    The rendered source binding compares the latest attempt to the current definition, which on a
    repeat failure reads as a match and says nothing about the prior attempt."""
    if not isinstance(data, dict):
        return
    run_id = data.get("workflow_run_id")
    latest = copilot_ctx.latest_recorded_build_test_outcome
    if not isinstance(run_id, str) or not isinstance(latest, RecordedBuildTestOutcome):
        return
    identity = prior_attempt_change_identity(
        copilot_ctx,
        attempted_block_label=latest.attempted_block_label or "",
        current_workflow_run_id=run_id,
    )
    if identity is None:
        return
    data["prior_attempt_change_identity"] = identity.model_dump(mode="json")
    LOG.info(
        "copilot carried prior-attempt change identity into failing run result",
        tool_name=tool_name,
        workflow_run_id=run_id,
        prior_workflow_run_id=identity.prior_workflow_run_id,
        block_label=identity.block_label,
        changed=identity.changed,
        basis=identity.basis,
    )


def _safe_reason_code(value: object) -> str:
    return value if isinstance(value, str) else ""


_RUN_SIDE_CONNECT_STATES: dict[str, BuildTestConnectFailureState] = {
    "browser_session_closed": "already_closed",
    "browser_session_startup_timeout": "provisioning_unavailable",
}


def _run_side_connect_state(failure_category: object) -> BuildTestConnectFailureState | None:
    if not isinstance(failure_category, list):
        return None
    for entry in failure_category:
        if isinstance(entry, dict) and entry.get("category") == "BROWSER_ERROR":
            state = _RUN_SIDE_CONNECT_STATES.get(str(entry.get("reason_code") or ""))
            if state is not None:
                return state
    return None


def _stamp_run_side_connect_failure(copilot_ctx: CopilotContext, result: dict[str, Any]) -> str | None:
    """Attach the typed browser-loss fact the run itself persisted at its lease seam, beside the
    run's own failure text, and return the sentence the user should see. Nothing here is inferred:
    a run that failed for any other reason carries no BROWSER_ERROR reason code and stays untyped,
    however its session row happens to look afterwards."""
    if result.get("ok"):
        return None
    data = result.get("data")
    if not isinstance(data, dict) or data.get("build_test_connect_failure") is not None:
        return None
    if data.get("blocks") or data.get("control_signal") is not None:
        return None
    workflow_run_id = data.get("workflow_run_id")
    if not isinstance(workflow_run_id, str) or not workflow_run_id:
        return None
    state = _run_side_connect_state(data.get("failure_category"))
    if state is None:
        return None
    session_id = data.get("browser_session_id")
    failure = BuildTestConnectFailure(
        state=state,
        browser_session_id=session_id if isinstance(session_id, str) and session_id else None,
        workflow_run_id=workflow_run_id,
    )
    data["build_test_connect_failure"] = failure.model_dump(mode="json", exclude_none=True)
    return build_test_connect_failure_sentence(failure)


def _is_budget_run_denial(result: Mapping[str, object]) -> bool:
    data = result.get("data")
    return isinstance(data, dict) and data.get("budget_expired") is True and data.get("run_dispatched") is False


async def _verify_and_record_run_blocks_result(
    copilot_ctx: Any, result: dict[str, Any], _handler_start: float
) -> RecordedBuildTestOutcome | None:
    """Record and emit the run fact once; no authoring judge may rewrite it."""
    if _is_budget_run_denial(result):
        # A denied dispatch is not a new failed run; retain the last actual run's evidence.
        return None
    if result.get(_INTERNAL_RUN_OUTCOME_RECORDED_KEY) is True:
        recorded = copilot_ctx.last_run_outcome
    else:
        recorded = _commit_run_blocks_record(copilot_ctx, result)
    if not result.get("ok"):
        _mark_stored_post_run_failure_page(copilot_ctx)
    if recorded is not None:
        await _send_run_outcome_update(
            copilot_ctx,
            result,
            verdict=recorded.verdict,
            role=recorded.role,
            reason_code=recorded.reason_code,
            display_reason=recorded.display_reason,
        )
    # The solver probe reaches the browser and can be cancelled by the turn deadline, so it runs
    # only after the outcome is committed and emitted, and still before the caller builds repair context.
    await _resolve_captcha_solver_availability(copilot_ctx)
    if recorded is None:
        return None
    build_test_outcome = getattr(copilot_ctx, "latest_recorded_build_test_outcome", None)
    result_data = result.get("data")
    result_run_id = result_data.get("workflow_run_id") if isinstance(result_data, dict) else None
    if isinstance(build_test_outcome, RecordedBuildTestOutcome) and build_test_outcome.workflow_run_id == result_run_id:
        return build_test_outcome
    return None


def _record_diagnosis_repair_contract(
    copilot_ctx: Any,
    *,
    source_tool: str,
    result: dict[str, Any],
    workflow_updated: bool = False,
) -> DiagnosisRepairContract:
    inject_runtime_authoring_repair_context(copilot_ctx, result)
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


def _packet_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _packet_string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []


def _packet_page_obstructions(value: Any, omission_notices: list[str]) -> list[PageObstruction]:
    if value is None:
        return []
    if not isinstance(value, list):
        omission_notices.append("failure.page_state.obstructions omitted: repair-context value was malformed.")
        return []
    obstructions: list[PageObstruction] = []
    malformed = 0
    for item in value:
        try:
            obstructions.append(PageObstruction.model_validate(model_visible_composition_evidence(item)))
        except ValueError:
            malformed += 1
    if malformed:
        omission_notices.append(
            f"failure.page_state.obstructions omitted: {malformed} malformed repair-context item(s)."
        )
    return obstructions


def _packet_workflow_readback(copilot_ctx: CopilotContext) -> tuple[str | None, str]:
    accepted = copilot_ctx.last_workflow_yaml
    if isinstance(accepted, str) and accepted.strip():
        return accepted, "accepted_write_readback"
    persisted = copilot_ctx.persisted_workflow_yaml
    if isinstance(persisted, str) and persisted.strip():
        return persisted, "turn_start_persisted_readback"
    return None, "unavailable"


def _packet_page_state(data: Mapping[str, Any], omission_notices: list[str]) -> BuildTestPacketPageState | None:
    repair_context = data.get("authoring_repair_context")
    run_id = _packet_string(data.get("workflow_run_id"))
    if run_id is None:
        if isinstance(repair_context, Mapping):
            omission_notices.append("failure.page_state omitted: page evidence has no recorded workflow run identity.")
        return None
    repair_run_id = (
        _packet_string(repair_context.get("workflow_run_id")) if isinstance(repair_context, Mapping) else None
    )
    if isinstance(repair_context, Mapping) and repair_run_id != run_id:
        omission_notices.append("failure.page_state omitted repair-context fields belonging to another or unknown run.")
    repair = repair_context if isinstance(repair_context, Mapping) and repair_run_id == run_id else {}
    omission_notices.extend(_packet_string_list(repair.get("page_obstruction_omission_notices")))
    current_url = _packet_string(repair.get("current_url")) or _packet_string(data.get("current_url"))
    title = _packet_string(repair.get("current_title")) or _packet_string(data.get("page_title"))
    page_state = BuildTestPacketPageState(
        current_origin=_packet_string(repair.get("current_origin")),
        current_url=current_url,
        title=title,
        evidence_source=_packet_string(repair.get("page_evidence_source")),
        observed_after_workflow_run=repair.get("observed_after_workflow_run") is True,
        rendered_value_excerpt=_packet_string(repair.get("rendered_value_excerpt")),
        form_summaries=_packet_string_list(repair.get("page_form_summaries")),
        result_summaries=_packet_string_list(repair.get("page_result_summaries")),
        action_summaries=_packet_string_list(repair.get("page_action_summaries")),
        challenge_summaries=_packet_string_list(repair.get("page_challenge_summaries")),
        obstruction_summaries=_packet_string_list(repair.get("page_obstruction_summaries")),
        obstructions=_packet_page_obstructions(repair.get("page_obstructions"), omission_notices),
    )
    repair_page_state = (
        page_state
        if any(
            (
                page_state.current_origin,
                page_state.current_url,
                page_state.title,
                page_state.evidence_source,
                page_state.rendered_value_excerpt,
                page_state.form_summaries,
                page_state.result_summaries,
                page_state.action_summaries,
                page_state.challenge_summaries,
                page_state.obstruction_summaries,
                page_state.obstructions,
            )
        )
        else None
    )
    if repair and repair_page_state is not None:
        return repair_page_state
    raw_page_evidence = data.get("post_run_page_evidence")
    if isinstance(raw_page_evidence, Mapping):
        evidence_page_state = build_test_page_state_from_evidence(raw_page_evidence, workflow_run_id=run_id)
        if evidence_page_state is not None:
            return evidence_page_state
    return repair_page_state


_OWN_BROWSER_NOTE = (
    "This run executed in a browser other than the one this chat's browser tools drive by default, "
    "so the page it left behind is not the page they answer from."
)
_SHARED_BROWSER_NOTE = "This run executed in this chat's browser, which the browser tools drive by default."


def _packet_run_browser(data: Mapping[str, Any]) -> BuildTestPacketRunBrowser | None:
    """Say which browser ran this, so a look at 'the page' can be aimed at the right one."""
    detached = data.get("run_detached_from_chat")
    if not isinstance(detached, bool):
        return None
    return BuildTestPacketRunBrowser(
        ran_outside_this_chats_browser=detached,
        note=_OWN_BROWSER_NOTE if detached else _SHARED_BROWSER_NOTE,
    )


def _packet_locator_observations(
    data: Mapping[str, Any], omission_notices: list[str]
) -> list[BuildTestPacketLocatorObservation]:
    raw = data.get("authored_locator_observations")
    if raw is None:
        omission_notices.append(
            "failure.locator_observations omitted: no post-action locator observation was attempted for this run."
        )
        return []
    observations: list[BuildTestPacketLocatorObservation] = []
    malformed = 0
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, Mapping):
            malformed += 1
            continue
        try:
            observations.append(BuildTestPacketLocatorObservation.model_validate(entry))
        except ValueError:
            malformed += 1
    if malformed:
        omission_notices.append(f"failure.locator_observations omitted {malformed} malformed item(s).")
    if not observations and not malformed:
        omission_notices.append("failure.locator_observations empty: the failed block names no literal locator.")
    return observations


def _packet_registered_outputs(
    copilot_ctx: CopilotContext,
    data: Mapping[str, Any],
    run_id: str | None,
    omission_notices: list[str],
) -> list[BuildTestPacketRegisteredOutput]:
    parameter_omission = _packet_string(data.get("registered_output_values_omission"))
    if parameter_omission is not None:
        omission_notices.append(f"requested output accounting omitted: {parameter_omission}.")
    raw_blocks = data.get("blocks")
    if run_id is None:
        return []
    if not isinstance(raw_blocks, list):
        raw_blocks = []
    recorded_outputs: list[BuildTestPacketRegisteredOutput] = []
    malformed = 0
    redacted = 0
    null_block_outputs = 0
    block_output_labels: set[str] = set()
    status_by_label: dict[str, str | None] = {}
    for raw_block in raw_blocks:
        if not isinstance(raw_block, Mapping):
            malformed += 1
            continue
        label = _packet_string(raw_block.get("label"))
        if label is not None:
            status_by_label.setdefault(label, _packet_string(raw_block.get("status")))
        if "output" not in raw_block:
            continue
        if label is not None:
            block_output_labels.add(label)
        if raw_block.get("output") is None:
            null_block_outputs += 1
            continue
        raw_output = raw_block.get("output")
        scrubbed_output = scrub_secrets_from_structure(copilot_ctx, raw_output)
        output_redacted = scrubbed_output != raw_output
        try:
            recorded_outputs.append(
                BuildTestPacketRegisteredOutput(
                    label=label,
                    status=_packet_string(raw_block.get("status")),
                    output=scrubbed_output,
                )
            )
            if output_redacted:
                redacted += 1
        except ValueError:
            malformed += 1

    # A loop can persist partial output directly on WorkflowRunOutputParameter before its
    # WorkflowRunBlock row receives an output. Preserve that same-run value in the compact packet
    # without replacing a real recorded block output when one exists.
    fallback_by_label: dict[str, dict[str, Any]] = {}
    fallback_redacted_labels: set[str] = set()
    other_run = 0
    unbound = 0
    raw_registered = data.get("registered_output_parameter_values")
    for raw_output in raw_registered if isinstance(raw_registered, list) else []:
        if not isinstance(raw_output, Mapping):
            malformed += 1
            continue
        if _packet_string(raw_output.get("workflow_run_id")) != run_id:
            other_run += 1
            continue
        label = _packet_string(raw_output.get("block_label"))
        key = _packet_string(raw_output.get("output_parameter_key"))
        if label is None or key is None:
            unbound += 1
            continue
        if label in block_output_labels:
            continue
        raw_value = raw_output.get("value")
        scrubbed_value = scrub_secrets_from_structure(copilot_ctx, raw_value)
        fallback_by_label.setdefault(label, {})[key] = scrubbed_value
        if scrubbed_value != raw_value:
            fallback_redacted_labels.add(label)
    fallback_outputs: list[BuildTestPacketRegisteredOutput] = []
    for label, fallback_output in fallback_by_label.items():
        try:
            fallback_outputs.append(
                BuildTestPacketRegisteredOutput(
                    label=label,
                    status=status_by_label.get(label),
                    output=fallback_output,
                )
            )
        except ValueError:
            malformed += 1
    # Partial-loop fallback is the irreplaceable evidence in a died-mid-loop run. Put it before
    # ordinary block outputs so both the normal 12-item cap and aggregate 6-item cap retain it.
    outputs = fallback_outputs + recorded_outputs
    if fallback_by_label:
        omission_notices.append(
            "registered_outputs included "
            f"{len(fallback_by_label)} item(s) reconstructed from same-run output-parameter rows "
            "because no workflow run block output was recorded."
        )
    redacted += len(fallback_redacted_labels)
    if malformed:
        omission_notices.append(f"registered_outputs omitted {malformed} non-JSON or malformed item(s).")
    if null_block_outputs:
        omission_notices.append(
            f"registered_outputs omitted {null_block_outputs} explicit null workflow run block output(s); "
            "registered output-parameter rows cannot replace them."
        )
    if other_run:
        omission_notices.append(f"registered_outputs omitted {other_run} item(s) belonging to another or unknown run.")
    if unbound:
        omission_notices.append(
            f"registered_outputs omitted {unbound} output-parameter item(s) with no run-snapshot block mapping."
        )
    if redacted:
        omission_notices.append(f"registered_outputs redacted {redacted} item(s) containing registered secret values.")
    return outputs


def _packet_requested_outputs(
    data: Mapping[str, Any], run_id: str | None, omission_notices: list[str]
) -> list[BuildTestPacketRequestedOutput]:
    raw_outputs = data.get("requested_output_parameter_definitions")
    if not isinstance(raw_outputs, list):
        omission = _packet_string(data.get("requested_output_definitions_omission"))
        if omission is not None:
            omission_notices.append(f"requested_outputs omitted: {omission}.")
        return []
    outputs: list[BuildTestPacketRequestedOutput] = []
    malformed = 0
    other_run = 0
    for raw_output in raw_outputs:
        if not isinstance(raw_output, Mapping):
            malformed += 1
            continue
        output_run_id = _packet_string(raw_output.get("workflow_run_id"))
        if run_id is None or output_run_id != run_id:
            other_run += 1
            continue
        try:
            outputs.append(
                BuildTestPacketRequestedOutput(
                    workflow_run_id=output_run_id,
                    output_parameter_id=_packet_string(raw_output.get("output_parameter_id")) or "",
                    output_parameter_key=_packet_string(raw_output.get("output_parameter_key")) or "",
                    description=_packet_string(raw_output.get("description")),
                    block_label=_packet_string(raw_output.get("block_label")),
                    block_type=_packet_string(raw_output.get("block_type")),
                )
            )
        except ValueError:
            malformed += 1
    if malformed:
        omission_notices.append(f"requested_outputs omitted {malformed} malformed item(s).")
    if other_run:
        omission_notices.append(f"requested_outputs omitted {other_run} item(s) belonging to another run.")
    return outputs


def _packet_downloads(
    copilot_ctx: CopilotContext,
    data: Mapping[str, Any],
    omission_notices: list[str],
) -> list[BuildTestPacketDownload]:
    run_id = _packet_string(data.get("workflow_run_id"))
    if run_id is None:
        return []
    raw_blocks = data.get("blocks")
    blocks = raw_blocks if isinstance(raw_blocks, list) else []
    outputs_by_label = {
        label: extracted
        for block in blocks
        if isinstance(block, Mapping)
        and isinstance((label := block.get("label")), str)
        and isinstance((extracted := block.get("extracted_data")), Mapping)
    }
    artifact_ids = _collect_downloaded_artifact_ids(outputs_by_label)
    evidence = copilot_ctx.registered_artifact_evidence
    names_by_id: dict[str, str | None] = {}
    if isinstance(evidence, RegisteredArtifactEvidence):
        if evidence.workflow_run_id == run_id:
            names_by_id = {entry.artifact_id: entry.file_name for entry in evidence.entries}
        elif artifact_ids:
            omission_notices.append("downloads omitted file names from artifact evidence belonging to another run.")
    return [
        BuildTestPacketDownload(artifact_id=artifact_id, file_name=names_by_id.get(artifact_id))
        for artifact_id in artifact_ids
    ]


def _packet_unfinished_items(
    copilot_ctx: CopilotContext,
    run_id: str | None,
    recorded_outcome: RecordedBuildTestOutcome | None,
    omission_notices: list[str],
) -> list[BuildTestPacketUnfinishedItem]:
    latest_outcome = getattr(copilot_ctx, "latest_recorded_build_test_outcome", None)
    outcome = recorded_outcome or (latest_outcome if isinstance(latest_outcome, RecordedBuildTestOutcome) else None)
    if run_id is None:
        if copilot_ctx.last_unverified_block_labels or outcome is not None:
            omission_notices.append("unfinished_items omitted: recorded unfinished evidence is not bound to this run.")
        return []
    outcome_matches_run = outcome is not None and outcome.workflow_run_id == run_id
    if not outcome_matches_run and (copilot_ctx.last_unverified_block_labels or outcome is not None):
        omission_notices.append("unfinished_items omitted: recorded unfinished evidence is not bound to this run.")
    unfinished: list[BuildTestPacketUnfinishedItem] = (
        [
            BuildTestPacketUnfinishedItem(kind="unverified_block", label=label)
            for label in dict.fromkeys(copilot_ctx.last_unverified_block_labels)
            if isinstance(label, str) and label
        ]
        if outcome_matches_run
        else []
    )
    # Keyed by owning block as well as path: two blocks can omit the same declared path, and
    # collapsing them here would tell repair to fix one and leave the other broken.
    missing_by_path: dict[tuple[str, str | None], str | None] = {}
    if outcome is not None and outcome.workflow_run_id == run_id:
        for fact in outcome.missing_requested_output_facts:
            output_path = _packet_string(fact.get("output_path"))
            if output_path is not None:
                block_label = _packet_string(fact.get("block_label"))
                missing_by_path[(output_path, block_label)] = _packet_string(fact.get("reason_code"))
    unfinished.extend(
        BuildTestPacketUnfinishedItem(
            kind="missing_requested_output",
            label=block_label,
            output_path=path,
            reason_code=reason_code,
        )
        for (path, block_label), reason_code in missing_by_path.items()
    )
    return unfinished


def build_test_evidence_packet(
    copilot_ctx: CopilotContext,
    result: Mapping[str, Any],
    *,
    recorded_outcome: RecordedBuildTestOutcome | None = None,
) -> BuildTestEvidencePacket:
    raw_data = result.get("data")
    data = raw_data if isinstance(raw_data, Mapping) else {}
    omission_notices: list[str] = []
    workflow_yaml, workflow_source = _packet_workflow_readback(copilot_ctx)
    if workflow_yaml is None:
        omission_notices.append(
            "canonical_workflow_yaml omitted: no accepted or turn-start persistence readback exists."
        )

    attempted_labels = _packet_string_list(data.get("requested_block_labels"))
    executed_labels = _packet_string_list(data.get("executed_block_labels"))
    run_id = _packet_string(data.get("workflow_run_id"))
    run_status = _packet_string(data.get("overall_status"))
    if run_id is None:
        omission_notices.append("run.workflow_run_id omitted: no workflow run was recorded for this result.")
    if run_status is None:
        omission_notices.append("run.status omitted: no recorded run status exists for this result.")
    if not attempted_labels:
        omission_notices.append("attempted_block_labels omitted: no block run attempt was recorded.")
    if not executed_labels:
        omission_notices.append("executed_block_labels omitted: no block execution was recorded.")

    raw_blocks = data.get("blocks")
    blocks = raw_blocks if isinstance(raw_blocks, list) else []
    failed_block = _first_failed_result([block for block in blocks if isinstance(block, Mapping)])
    action_trace = _packet_string_list(data.get("action_trace_summary"))
    action_observations = _packet_string_list(data.get("action_observations"))
    if not action_observations:
        omission_notices.append("action_observations empty: no same-run action observation was recorded.")
    page_state = _packet_page_state(data, omission_notices)
    raw_page_evidence = data.get("post_run_page_evidence")
    page_capture = post_run_page_capture_from_result(
        data,
        raw_page_evidence if isinstance(raw_page_evidence, Mapping) else None,
    )
    if page_capture is None and recorded_outcome is not None:
        page_capture = recorded_outcome.page_capture
    failure: BuildTestPacketFailure | None = None
    if failed_block is not None or result.get("ok") is False:
        connect_failure = (
            recorded_outcome.connect_failure
            if recorded_outcome is not None
            else connect_failure_from_run_blocks_result(result)
        )
        if recorded_outcome is not None and recorded_outcome.workflow_run_id == run_id:
            failed_operation = recorded_outcome.failed_operation
            if failed_operation is not None and failed_operation.workflow_run_id != run_id:
                failed_operation = None
        else:
            failed_operation = failed_operation_from_run_blocks_result(result)
        if failed_operation is not None:
            if failed_operation.workflow_run_id is None:
                omission_notices.append(
                    "failure.failed_operation.workflow_run_id omitted: no run identity was recorded for the operation."
                )
            if failed_operation.workflow_run_block_id is None:
                omission_notices.append(
                    "failure.failed_operation.workflow_run_block_id omitted: no run-block identity was recorded for "
                    "the operation."
                )
            if failed_operation.block_label is None:
                omission_notices.append(
                    "failure.failed_operation.block_label omitted: no block label was recorded for the operation."
                )
            if failed_operation.failing_line is None:
                omission_notices.append(
                    "failure.failed_operation.failing_line omitted: no failing code line was recorded for the operation."
                )
        failed_output = failed_block.get("output") if failed_block is not None else None
        failure_page_state = failed_output.get("failure_page_state") if isinstance(failed_output, Mapping) else None
        if not isinstance(failure_page_state, Mapping):
            failure_page_state = {}
        failure = BuildTestPacketFailure(
            final_url=_packet_string(failure_page_state.get("final_url")),
            page_title=_packet_string(failure_page_state.get("page_title")),
            covering_element=_packet_string(failure_page_state.get("covering_element")),
            workflow_run_block_id=(
                _packet_string(failed_block.get("workflow_run_block_id")) if failed_block is not None else None
            ),
            task_id=_packet_string(failed_block.get("task_id")) if failed_block is not None else None,
            step_id=_packet_string(failed_block.get("step_id")) if failed_block is not None else None,
            block_label=_packet_string(failed_block.get("label")) if failed_block is not None else None,
            block_type=_packet_string(failed_block.get("block_type")) if failed_block is not None else None,
            block_status=_packet_string(failed_block.get("status")) if failed_block is not None else run_status,
            reason=(
                _packet_string(failed_block.get("failure_reason"))
                if failed_block is not None
                else _packet_string(data.get("failure_reason")) or _packet_string(result.get("error"))
            ),
            error_codes=(_packet_string_list(failed_block.get("error_codes")) if failed_block is not None else []),
            failing_line=data.get("failing_code_line") if type(data.get("failing_code_line")) is int else None,
            failed_operation=failed_operation,
            connect_failure=connect_failure,
            action_trace=action_trace,
            page_state=page_state,
            locator_observations=_packet_locator_observations(data, omission_notices),
        )
        if not action_trace:
            omission_notices.append("failure.action_trace omitted: no failed action was recorded.")
        if page_state is None:
            omission_notices.append("failure.page_state omitted: no bounded same-run page state was recorded.")
        if failure.reason is None:
            omission_notices.append("failure.reason omitted: no recorded failure reason exists.")
    else:
        omission_notices.append("failure omitted: no failed run or failed block was recorded.")
        terminal_omission = _packet_string(data.get("terminal_page_evidence_omission"))
        if terminal_omission is not None:
            omission_notices.append(f"page_state terminal artifact omitted: {terminal_omission}.")
        elif page_state is None:
            omission_notices.append("page_state omitted: no bounded same-run terminal page state was recorded.")

    requested_outputs = _packet_requested_outputs(data, run_id, omission_notices)
    registered_outputs = _packet_registered_outputs(copilot_ctx, data, run_id, omission_notices)
    if not registered_outputs:
        omission_notices.append(
            "registered_outputs empty: no workflow run block output or registered output-parameter value was recorded."
        )
    downloads = _packet_downloads(copilot_ctx, data, omission_notices)
    if not downloads:
        omission_notices.append("downloads empty: no registered download artifact was recorded.")

    screenshot_present = (
        run_id is not None and isinstance(data.get("screenshot_base64"), str) and bool(data.get("screenshot_base64"))
    )
    screenshot_provenance = "data.screenshot_base64" if screenshot_present else None
    if not screenshot_present and run_id is not None:
        failed_screenshot = next(
            (
                block
                for block in blocks
                if isinstance(block, Mapping)
                and isinstance(block.get("screenshot_b64"), str)
                and bool(block.get("screenshot_b64"))
            ),
            None,
        )
        if failed_screenshot is not None:
            screenshot_present = True
            screenshot_provenance = "data.blocks[].screenshot_b64"
    if not screenshot_present:
        omission_notices.append("screenshot omitted: no final or failed-block screenshot was recorded.")

    unfinished_items = _packet_unfinished_items(
        copilot_ctx,
        run_id,
        recorded_outcome,
        omission_notices,
    )
    if not unfinished_items:
        omission_notices.append("unfinished_items empty: recorded outcome and workflow evidence identify none.")

    return BuildTestEvidencePacket(
        workflow_permanent_id=copilot_ctx.workflow_permanent_id,
        canonical_workflow_yaml=workflow_yaml,
        canonical_workflow_source=workflow_source,
        canonical_workflow_yaml_complete=workflow_yaml is not None,
        attempted_block_labels=attempted_labels,
        executed_block_labels=executed_labels,
        run=BuildTestPacketRun(
            workflow_run_id=run_id,
            browser_session_id=_packet_string(data.get("browser_session_id")),
            status=run_status,
            browser=_packet_run_browser(data),
        ),
        action_observations=action_observations,
        failure=failure,
        page_state=page_state if failure is None else None,
        page_capture=page_capture,
        requested_outputs=requested_outputs,
        registered_outputs=registered_outputs,
        downloads=downloads,
        screenshot=BuildTestPacketScreenshot(present=screenshot_present, provenance=screenshot_provenance),
        unfinished_items=unfinished_items,
        omission_notices=omission_notices,
    )


def finalize_build_test_result(
    copilot_ctx: CopilotContext,
    *,
    source_tool: str,
    result: dict[str, Any],
    workflow_updated: bool = False,
    diagnosis_shadow_eligible: bool = True,
    recorded_outcome: RecordedBuildTestOutcome | None = None,
) -> dict[str, Any]:
    """Finalize diagnosis state and attach the one shared factual packet."""
    if _is_budget_run_denial(result):
        return result
    # Tool seams sometimes deliberately suppress collection and return no recorded outcome.
    # Treat any non-record as absent rather than allowing a test double or failed observer to
    # contaminate the model-facing packet.
    if not isinstance(recorded_outcome, RecordedBuildTestOutcome):
        recorded_outcome = None
    if diagnosis_shadow_eligible:
        _record_diagnosis_repair_contract(
            copilot_ctx,
            source_tool=source_tool,
            result=result,
            workflow_updated=workflow_updated,
        )
    data = result.get("data")
    if not isinstance(data, dict):
        data = {}
        result["data"] = data
    data[BUILD_TEST_PACKET_KEY] = scrub_secrets_from_structure(
        copilot_ctx,
        build_test_evidence_packet(
            copilot_ctx,
            result,
            recorded_outcome=recorded_outcome,
        ).model_dump(mode="json", exclude_none=True),
    )
    return result


async def hydrate_prior_run_packet(
    copilot_ctx: CopilotContext,
    *,
    workflow_run_id: str | None,
) -> dict[str, Any] | None:
    """The persisted facts of a run this turn did not perform, as the one shared packet. Diagnosis
    shadow recording is off: nothing was diagnosed here, the run already happened."""
    if not workflow_run_id:
        return None
    # A turn that cannot read its origin run still has to run. Reading, validating and projecting
    # all sit inside this, so a failure anywhere degrades to no packet rather than ending the turn.
    try:
        result = await _get_run_results(
            {"workflow_run_id": workflow_run_id},
            copilot_ctx,
            read_live_page=False,
            admit_sensitive_origin_artifact=False,
        )
        if result.get("ok") is False:
            return None
        recorded_outcome = _build_recorded_build_test_outcome(copilot_ctx, result, recorded_run_outcome=None)
        finalized = finalize_build_test_result(
            copilot_ctx,
            source_tool="get_run_results",
            result=result,
            diagnosis_shadow_eligible=False,
            recorded_outcome=recorded_outcome,
        )
        data = finalized.get("data")
        packet = data.get(BUILD_TEST_PACKET_KEY) if isinstance(data, dict) else None
        if not isinstance(packet, dict):
            return None
        # The same bounded projection the tool path applies. This one is rendered into the user
        # turn, which the input budget does not trim, so an unbounded packet would reach the model.
        projected = project_build_test_packet_for_llm(BuildTestEvidencePacket.model_validate(packet)).model_dump(
            mode="json", exclude_none=True
        )
    except Exception:
        LOG.warning("copilot prior-run packet unavailable", workflow_run_id=workflow_run_id, exc_info=True)
        return None
    # The prompt already carries the workflow in its own section; a second copy here would sit
    # unlabelled beside it, and disagree with it whenever the editor holds an unsaved draft.
    for field in ("canonical_workflow_yaml", "canonical_workflow_yaml_complete", "canonical_workflow_source"):
        projected.pop(field, None)
    notices = projected.get("omission_notices")
    if isinstance(notices, list):
        kept = [n for n in notices if not (isinstance(n, str) and "canonical_workflow_yaml" in n)]
        if kept:
            projected["omission_notices"] = kept
        else:
            projected.pop("omission_notices", None)
    return projected


def _diagnosis_repair_tool_error(copilot_ctx: Any, source_tool: str, error: str) -> str:
    result = {"ok": False, "error": error}
    finalize_build_test_result(copilot_ctx, source_tool=source_tool, result=result)
    return json.dumps(sanitize_tool_result_for_llm(source_tool, result))


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
        "block_count": len(block_labels),
    }

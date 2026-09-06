"""Copilot agent tools — native handlers, hooks, and registration."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import structlog
from agents import FunctionTool, function_tool
from agents.run_context import RunContextWrapper
from agents.tool_context import ToolContext

from skyvern.forge import app as app
from skyvern.forge.sdk.copilot.ask_user import AskUserArguments, QuestionInput
from skyvern.forge.sdk.copilot.browser_target import resolve_browser_session_binding
from skyvern.forge.sdk.copilot.composition_evidence import (
    composition_page_evidence_error as composition_page_evidence_error,
)
from skyvern.forge.sdk.copilot.composition_evidence import has_bounded_page_schema as has_bounded_page_schema
from skyvern.forge.sdk.copilot.composition_evidence import (
    normalize_block_observation_refs,
)
from skyvern.forge.sdk.copilot.composition_evidence import workflow_target_url as workflow_target_url
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.credential_pause import (
    await_pending_credential_pause,
    release_credential_pause_gate,
)
from skyvern.forge.sdk.copilot.enforcement import requested_output_paths_for_derivation
from skyvern.forge.sdk.copilot.loop_detection import record_tool_step_result_for_ctx
from skyvern.forge.sdk.copilot.output_extraction_plan import (
    requested_output_designation_capability,
    value_designation_probe_expression,
)
from skyvern.forge.sdk.copilot.output_utils import (
    _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY as _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
)
from skyvern.forge.sdk.copilot.output_utils import (
    sanitize_tool_result_for_llm,
)
from skyvern.forge.sdk.copilot.pending_operation import pending_operation
from skyvern.forge.sdk.copilot.runtime import (
    SENSITIVE_ORIGIN_PAGE_ERROR,
    bound_call_browser_session,
    resolve_browser_state_for_context,
    sensitive_origin_page_facts_withheld,
)
from skyvern.forge.sdk.copilot.screenshot_utils import (
    ScreenshotActionRelation,
    ScreenshotProvenance,
    enqueue_screenshot_from_result,
)
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure
from skyvern.forge.sdk.copilot.tools.locator_inspection import TOOL_DESCRIPTION as LOCATOR_INSPECTION_TOOL_DESCRIPTION
from skyvern.forge.sdk.copilot.tools.locator_inspection import TOOL_NAME as LOCATOR_INSPECTION_TOOL_NAME
from skyvern.forge.sdk.copilot.tools.locator_inspection import TOOL_SCHEMA as LOCATOR_INSPECTION_TOOL_SCHEMA
from skyvern.forge.sdk.copilot.tools.locator_inspection import (
    inspect_locator_matches,
)
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.workflow_yaml import (
    BlockEditError,
)
from skyvern.forge.sdk.copilot.workflow_yaml import _process_workflow_yaml as _process_workflow_yaml
from skyvern.forge.sdk.copilot.workflow_yaml import (
    add_block_to_workflow,
    apply_block_edit,
    delete_block_from_workflow,
    stored_workflow_yaml,
)

from ._shared import _COMPOSITION_STRIPPED_HTML_MAX_CHARS as _COMPOSITION_STRIPPED_HTML_MAX_CHARS
from ._shared import _DISCOVERY_PER_CALL_TIMEOUT_SECONDS as _DISCOVERY_PER_CALL_TIMEOUT_SECONDS
from ._shared import _FAILED_BLOCK_STATUSES as _FAILED_BLOCK_STATUSES
from ._shared import BLOCK_RUNNING_TOOLS as BLOCK_RUNNING_TOOLS
from ._shared import RUN_BLOCKS_SAFETY_CEILING_SECONDS as RUN_BLOCKS_SAFETY_CEILING_SECONDS
from ._shared import AdmittedOutputRead as AdmittedOutputRead
from ._shared import RequestedOutputRead as RequestedOutputRead
from ._shared import _composition_get_html as _composition_get_html
from ._shared import _current_workflow_has_evidence_block as _current_workflow_has_evidence_block
from ._shared import _fallback_page_info as _fallback_page_info
from ._shared import _is_meaningful_extracted_data as _is_meaningful_extracted_data
from ._shared import _proxy_location_trace_value as _proxy_location_trace_value
from ._shared import _raw_yaml_proxy_location as _raw_yaml_proxy_location
from ._shared import _same_page_ignoring_fragment as _same_page_ignoring_fragment
from ._shared import _unverified_current_workflow_labels as _unverified_current_workflow_labels
from ._shared import admitted_requested_output_reads
from .banned_blocks import _COPILOT_BANNED_BLOCK_TYPES as _COPILOT_BANNED_BLOCK_TYPES
from .banned_blocks import _banned_block_reject_message as _banned_block_reject_message
from .banned_blocks import _detect_new_banned_blocks as _detect_new_banned_blocks
from .banned_blocks import _record_banned_block_reject_span as _record_banned_block_reject_span
from .blockers import _analyze_run_blocks as _analyze_run_blocks
from .blockers import _run_blocks_structured_blocker_message as _run_blocks_structured_blocker_message
from .blockers import _trusted_post_drain_status as _trusted_post_drain_status
from .completion import _build_run_evidence_snapshot as _build_run_evidence_snapshot
from .completion import _completion_verification_handler as _completion_verification_handler
from .completion import _is_outcome_evidence_candidate as _is_outcome_evidence_candidate
from .completion import _is_unfinished_run_verification_candidate as _is_unfinished_run_verification_candidate
from .completion import (
    _maybe_run_completion_verification_from_page_observation as _maybe_run_completion_verification_from_page_observation,
)
from .completion import _stamp_turn_budget_on_result as _stamp_turn_budget_on_result
from .composition_capture import _capture_composition_evidence as _capture_composition_evidence
from .composition_capture import (
    _composition_evidence_after_navigation_failure as _composition_evidence_after_navigation_failure,
)
from .composition_capture import _composition_visual_handler as _composition_visual_handler
from .composition_capture import _composition_visual_prompt as _composition_visual_prompt
from .composition_capture import (
    _inspect_page_for_composition_impl,
    _model_facing_inspect_result,
)
from .composition_capture import _normalized_inspect_url as _normalized_inspect_url
from .composition_capture import _same_inspect_target as _same_inspect_target
from .credential_fill import _credential_fill_authority_error as _credential_fill_authority_error
from .credential_fill import _credential_fill_prerequisite_error as _credential_fill_prerequisite_error
from .credential_fill import (
    _fill_credential_field_impl,
    _request_credential,
)
from .credential_fill import _resolve_credential_fill_value as _resolve_credential_fill_value
from .credentials import _credential_id_misbinding_findings as _credential_id_misbinding_findings
from .credentials import _credential_reference_validation_error as _credential_reference_validation_error
from .credentials import _extract_credential_ids_from_tool_value as _extract_credential_ids_from_tool_value
from .credentials import _extract_credential_ids_from_workflow_yaml as _extract_credential_ids_from_workflow_yaml
from .credentials import (
    _list_credentials,
)
from .discovery import _DISCOVERY_NAVIGATION_FALLBACK_CONFIDENCE as _DISCOVERY_NAVIGATION_FALLBACK_CONFIDENCE
from .discovery import _DISCOVERY_STEP_CAP as _DISCOVERY_STEP_CAP
from .discovery import _discover_workflow_entrypoint_impl as _discover_workflow_entrypoint_impl
from .discovery import _discovery_anchor_score as _discovery_anchor_score
from .discovery import _discovery_click_anchor as _discovery_click_anchor
from .discovery import _discovery_detect_anti_bot as _discovery_detect_anti_bot
from .discovery import _discovery_detect_login_wall as _discovery_detect_login_wall
from .discovery import _discovery_resolve_href as _discovery_resolve_href
from .discovery import _discovery_walk as _discovery_walk
from .discovery import _rank_discovery_entrypoint_candidates as _rank_discovery_entrypoint_candidates
from .discovery import _resolve_discovery_entry_url as _resolve_discovery_entry_url
from .frontier import _CANONICAL_WORKFLOW_SETTING_FIELDS as _CANONICAL_WORKFLOW_SETTING_FIELDS
from .frontier import _JINJA_LITERAL_ROOTS as _JINJA_LITERAL_ROOTS
from .frontier import _JINJA_RUNTIME_GLOBAL_ROOTS as _JINJA_RUNTIME_GLOBAL_ROOTS
from .frontier import _JINJA_SPECIAL_CONTEXT_ROOTS as _JINJA_SPECIAL_CONTEXT_ROOTS
from .frontier import _SKYVERN_TEMPLATE_CONTEXT_ROOTS as _SKYVERN_TEMPLATE_CONTEXT_ROOTS
from .frontier import _TEMPLATE_BUILTIN_ROOTS as _TEMPLATE_BUILTIN_ROOTS
from .frontier import _detect_stale_block_metadata as _detect_stale_block_metadata
from .frontier import _find_invalidated_labels as _find_invalidated_labels
from .frontier import _frontier_runtime_page_url as _frontier_runtime_page_url
from .frontier import _get_prior_workflow as _get_prior_workflow
from .frontier import _get_prior_workflow_definition as _get_prior_workflow_definition
from .frontier import _invalidate_verified_state_on_edit as _invalidate_verified_state_on_edit
from .frontier import _plan_frontier as _plan_frontier
from .frontier import _referenced_output_labels as _referenced_output_labels
from .frontier import _stale_block_metadata_message as _stale_block_metadata_message
from .frontier import _unknown_jinja_roots as _unknown_jinja_roots
from .frontier import _workflow_requires_canonical_persist as _workflow_requires_canonical_persist
from .frontier import _workflow_with_runtime_frontier_anchor as _workflow_with_runtime_frontier_anchor
from .frontier import (
    _workflow_with_runtime_frontier_starter_url_seed as _workflow_with_runtime_frontier_starter_url_seed,
)
from .guardrails import (
    _WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL,
    _authority_tool_error,
    _credential_deferred_draft_requires_skipped_run,
)
from .guardrails import _parameter_binding_invariant_error as _parameter_binding_invariant_error
from .guardrails import (
    _update_and_run_requires_skipped_run,
)
from .integrations import (
    _list_integrations,
)
from .mcp_hooks import _build_skyvern_mcp_overlays as _build_skyvern_mcp_overlays
from .mcp_hooks import _click_post_hook as _click_post_hook
from .mcp_hooks import _click_pre_hook as _click_pre_hook
from .mcp_hooks import _evaluate_post_hook as _evaluate_post_hook
from .mcp_hooks import _get_block_schema_post_hook as _get_block_schema_post_hook
from .mcp_hooks import _get_block_schema_pre_hook as _get_block_schema_pre_hook
from .mcp_hooks import _navigate_post_hook as _navigate_post_hook
from .mcp_hooks import _press_key_post_hook as _press_key_post_hook
from .mcp_hooks import _screenshot_post_hook as _screenshot_post_hook
from .mcp_hooks import _select_option_post_hook as _select_option_post_hook
from .mcp_hooks import _type_text_post_hook as _type_text_post_hook
from .mcp_hooks import _verify_scout_type_landed as _verify_scout_type_landed
from .mcp_hooks import get_skyvern_mcp_alias_map as get_skyvern_mcp_alias_map
from .page_observation import _record_composition_page_observation as _record_composition_page_observation
from .page_observation import _resolve_url_title as _resolve_url_title
from .run_execution import RUN_BLOCKS_STAGNATION_WINDOW_SECONDS as RUN_BLOCKS_STAGNATION_WINDOW_SECONDS
from .run_execution import WatchdogExitReason as WatchdogExitReason
from .run_execution import _any_quiet_block_requested as _any_quiet_block_requested
from .run_execution import _attach_action_traces as _attach_action_traces
from .run_execution import _block_end_urls_by_label as _block_end_urls_by_label
from .run_execution import _cancel_run_task_if_not_final as _cancel_run_task_if_not_final
from .run_execution import (
    _carry_unresolved_failure_into_result,
)
from .run_execution import _composition_anti_bot_reason as _composition_anti_bot_reason
from .run_execution import _detect_non_retriable_nav_error as _detect_non_retriable_nav_error
from .run_execution import (
    _diagnosis_repair_tool_error,
    _get_run_results,
)
from .run_execution import _progress_marker as _progress_marker
from .run_execution import _read_progress_sources as _read_progress_sources
from .run_execution import _record_diagnosis_repair_contract as _record_diagnosis_repair_contract
from .run_execution import _record_run_blocks_result as _record_run_blocks_result
from .run_execution import (
    _run_blocks_and_collect_debug,
    _run_blocks_span_data,
    _verify_and_record_run_blocks_result,
)
from .run_execution import _watchdog_error_message as _watchdog_error_message
from .run_execution import (
    finalize_build_test_result,
)
from .scouting import _MAX_SCOUTED_INTERACTIONS as _MAX_SCOUTED_INTERACTIONS
from .scouting import _capture_accessible_role_name as _capture_accessible_role_name
from .scouting import _capture_scout_pre_action as _capture_scout_pre_action
from .scouting import _capture_scout_source_url as _capture_scout_source_url
from .scouting import _clear_pending_browser_interaction_observation as _clear_pending_browser_interaction_observation
from .scouting import (
    _consume_pending_browser_interaction_observation as _consume_pending_browser_interaction_observation,
)
from .scouting import _consume_scout_source_url as _consume_scout_source_url
from .scouting import _mark_pending_browser_interaction_observation as _mark_pending_browser_interaction_observation
from .scouting import _mark_post_run_page_observed as _mark_post_run_page_observed
from .scouting import _prenav_ambiguity_for_selector as _prenav_ambiguity_for_selector
from .scouting import _prenav_role_name_for_selector as _prenav_role_name_for_selector
from .scouting import _record_scouted_interaction as _record_scouted_interaction
from .scouting import _register_scout_interaction_observation as _register_scout_interaction_observation
from .scouting import _resolve_scout_role_name as _resolve_scout_role_name
from .scouting import _role_name_from_selector as _role_name_from_selector
from .workflow_update import BlockObservationRef as BlockObservationRef
from .workflow_update import CodeArtifactMetadata as CodeArtifactMetadata
from .workflow_update import _code_artifact_metadata_as_tool_argument as _code_artifact_metadata_as_tool_argument
from .workflow_update import _code_block_safety_errors as _code_block_safety_errors
from .workflow_update import _normalize_code_artifact_metadata as _normalize_code_artifact_metadata
from .workflow_update import _record_workflow_proxy_location_span as _record_workflow_proxy_location_span
from .workflow_update import _record_workflow_update_result as _record_workflow_update_result
from .workflow_update import _update_workflow as _update_workflow
from .workflow_update import carry_author_time_findings as carry_author_time_findings

LOG = structlog.get_logger()

_CREDENTIAL_DEFERRED_DRAFT_MESSAGE = (
    "I can save this as a draft without running it because the credentials aren't set up yet. "
    "Add them in the Credentials UI and ask me to test the workflow."
)


def _originating_call_id(ctx: RunContextWrapper) -> str | None:
    """The id of the tool call currently executing, so a write's diff can be handed back to that
    call's result rather than to whichever code-write result arrives first. The SDK passes a
    ``ToolContext`` at call time; the annotation is its base class, so narrow before reading."""
    return ctx.tool_call_id if isinstance(ctx, ToolContext) else None


def _mark_credential_deferred_draft(copilot_ctx: CopilotContext, result: dict[str, Any]) -> None:
    """Credential-deferred drafts persist without a run, so they carry the same skip markers and
    set the same flag the combined tool's skip branch does — credential-pause routing reads it."""
    copilot_ctx.last_run_skipped_unbound_credentials = True
    data = result.get("data")
    if not isinstance(data, dict):
        data = {}
        result["data"] = data
    data["skipped_run"] = True
    data["skip_reason"] = "workflow_credential_inputs_unbound"
    data["message"] = _CREDENTIAL_DEFERRED_DRAFT_MESSAGE


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

    A successful write is staged as a proposal, which the returned `persistence` and
    `persistence_message` report. The user accepts a staged proposal to save it, or
    discards it to keep the current version.

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
    # Mirrors the combined tool: a stale True from an earlier call in the same turn would
    # misreport this call's authoring error as a credential ask.
    copilot_ctx.last_run_skipped_unbound_credentials = False
    serialized_code_artifact_metadata: object = _code_artifact_metadata_as_tool_argument(code_artifact_metadata)
    normalized_block_observation_refs = normalize_block_observation_refs(block_observation_refs)
    arguments = {
        "workflow_yaml": workflow_yaml,
        "block_observation_refs": normalized_block_observation_refs,
        "code_artifact_metadata": serialized_code_artifact_metadata,
    }
    credential_deferred_draft = _credential_deferred_draft_requires_skipped_run(copilot_ctx)

    prior_definition = await _get_prior_workflow_definition(copilot_ctx)
    with copilot_span("update_workflow", data={"yaml_length": len(workflow_yaml)}):
        result = await _update_workflow(
            {
                **arguments,
                "raw_block_observation_refs": block_observation_refs,
                "raw_code_artifact_metadata": code_artifact_metadata,
            },
            copilot_ctx,
            allow_missing_credentials=credential_deferred_draft
            or getattr(copilot_ctx, "allow_untested_workflow_draft", False) is True,
            originating_call_id=_originating_call_id(ctx),
        )
        if credential_deferred_draft and result.get("ok"):
            _mark_credential_deferred_draft(copilot_ctx, result)
        _record_workflow_update_result(copilot_ctx, result, prior_definition)
        record_tool_step_result_for_ctx(copilot_ctx, "update_workflow", arguments, result)
        finalize_build_test_result(
            copilot_ctx,
            source_tool="update_workflow",
            result=result,
            workflow_updated=result.get("ok") is True,
            diagnosis_shadow_eligible=result.get("ok") is False,
        )
    sanitized = sanitize_tool_result_for_llm("update_workflow", result)
    return json.dumps(sanitized)


async def _persist_block_scoped_edit(
    copilot_ctx: Any,
    tool_name: str,
    workflow_yaml: str,
    arguments: dict,
    *,
    originating_call_id: str | None = None,
    code_artifact_metadata: list[CodeArtifactMetadata] | None = None,
    block_observation_refs: list[BlockObservationRef] | None = None,
) -> str:
    """Send a server-composed workflow through the normal persistence path.

    The model never sends the whole workflow, but the saved result still goes through every
    author-time check, so a block edit cannot slip past what a full submission must satisfy.
    """
    prior_definition = await _get_prior_workflow_definition(copilot_ctx)
    params: dict[str, Any] = {"workflow_yaml": workflow_yaml, "_preserve_code_block_associations": True}
    if code_artifact_metadata is not None:
        params["code_artifact_metadata"] = _code_artifact_metadata_as_tool_argument(code_artifact_metadata)
        params["raw_code_artifact_metadata"] = code_artifact_metadata
    if block_observation_refs is not None:
        params["block_observation_refs"] = normalize_block_observation_refs(block_observation_refs)
        params["raw_block_observation_refs"] = block_observation_refs
    with copilot_span(tool_name, data={"yaml_length": len(workflow_yaml)}):
        result = await _update_workflow(params, copilot_ctx, originating_call_id=originating_call_id)
        _record_workflow_update_result(copilot_ctx, result, prior_definition)
        record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, result)
        finalize_build_test_result(
            copilot_ctx,
            source_tool=tool_name,
            result=result,
            workflow_updated=result.get("ok") is True,
            diagnosis_shadow_eligible=result.get("ok") is False,
        )
    return json.dumps(sanitize_tool_result_for_llm(tool_name, result))


def _stored_workflow_yaml(copilot_ctx: Any) -> str:
    return stored_workflow_yaml(copilot_ctx)


@function_tool(name_override="edit_block", strict_mode=False)
async def edit_block_tool(
    ctx: RunContextWrapper,
    label: str,
    expected_code: str | None = None,
    replacement_code: str | None = None,
    fields: dict[str, Any] | None = None,
) -> str:
    """Change one block, leaving every other block exactly as it is.

    Prefer this over update_and_run_blocks whenever you are changing an existing block: you send only
    the change, so a block that already works cannot be disturbed and the workflow is not retyped.

    For a `code` block, pass `expected_code` (a snippet of its current code, unique within that block)
    and `replacement_code`. The edit is rejected if the snippet is missing or appears more than once,
    which is how an edit written against a stale copy of the block fails instead of overwriting newer
    code. Read the block first if you are unsure what it currently contains.

    For other settings, pass `fields` with just the keys to change (e.g. a navigation goal or url).

    To remove a block use delete_block. Omitting a block here never deletes it.
    """
    copilot_ctx = ctx.context
    arguments = {"label": label, "fields": fields, "has_code_edit": expected_code is not None}
    authority_error = _authority_tool_error(copilot_ctx, "edit_block")
    if authority_error:
        result = {"ok": False, "error": authority_error}
        finalize_build_test_result(
            copilot_ctx,
            source_tool="edit_block",
            result=result,
            diagnosis_shadow_eligible=False,
        )
        return json.dumps(sanitize_tool_result_for_llm("edit_block", result))
    try:
        workflow_yaml = apply_block_edit(
            _stored_workflow_yaml(copilot_ctx),
            label,
            expected_code=expected_code,
            replacement_code=replacement_code,
            fields=fields,
        )
    except BlockEditError as exc:
        result = {"ok": False, "error": str(exc)}
        record_tool_step_result_for_ctx(copilot_ctx, "edit_block", arguments, result)
        finalize_build_test_result(
            copilot_ctx,
            source_tool="edit_block",
            result=result,
            diagnosis_shadow_eligible=False,
        )
        return json.dumps(sanitize_tool_result_for_llm("edit_block", result))
    return await _persist_block_scoped_edit(
        copilot_ctx, "edit_block", workflow_yaml, arguments, originating_call_id=_originating_call_id(ctx)
    )


@function_tool(
    name_override="edit_block_and_run",
    timeout=RUN_BLOCKS_SAFETY_CEILING_SECONDS,
    strict_mode=False,
)
async def edit_block_and_run_tool(
    ctx: RunContextWrapper,
    label: str,
    expected_code: str,
    replacement_code: str,
    block_labels: list[str] | None = None,
    parameters: dict[str, Any] | None = None,
) -> str:
    """Apply one anchored code edit and immediately test the affected frontier.

    Use this for a repair to an existing code block. ``label`` must name exactly one existing block,
    and ``expected_code`` must occur exactly once in its current stored code. The tool changes only
    that code span, persists the reversible draft through the normal author-time safety boundary,
    then runs ``block_labels`` (or just ``label`` when omitted).

    This is one model-invoked edit and one run. It does not choose an edit, create a block, retry, or
    decide whether the result achieved the user's goal. Its response is the same sanitized run/debug
    evidence returned by ``run_blocks_and_collect_debug``; a failed run still leaves the edited draft
    and recorded workflow run available for the next turn.

    Pass current non-secret values for runtime workflow parameters in ``parameters``. For sensitive
    values (password, secret, token, api_key, credential, totp, otp, one_time_code, private_key,
    auth), do NOT pass an inline value. Ask the user to store it as a saved
    credential and reply with the credential name; do not build or run with the raw value.
    """
    copilot_ctx = ctx.context
    await await_pending_credential_pause(copilot_ctx)
    copilot_ctx.completion_verification_result = None
    handler_start = time.monotonic()
    requested_labels = list(block_labels) if block_labels else [label]
    runtime_parameters = parameters or {}
    arguments = {
        "label": label,
        "block_labels": requested_labels,
        "parameters": runtime_parameters,
        "has_code_edit": True,
    }
    skip_run_after_update = _update_and_run_requires_skipped_run(copilot_ctx, "edit_block_and_run")
    copilot_ctx.last_run_skipped_unbound_credentials = False
    if label not in requested_labels:
        result = {
            "ok": False,
            "error": f"block_labels must include the edited block {label!r} so this call tests the persisted repair.",
        }
        _carry_unresolved_failure_into_result(copilot_ctx, result, "edit_block_and_run")
        record_tool_step_result_for_ctx(copilot_ctx, "edit_block_and_run", arguments, result)
        finalize_build_test_result(
            copilot_ctx,
            source_tool="edit_block_and_run",
            result=result,
            diagnosis_shadow_eligible=False,
        )
        return json.dumps(sanitize_tool_result_for_llm("edit_block_and_run", result))
    authority_error = _authority_tool_error(copilot_ctx, "edit_block_and_run")
    if authority_error:
        return _diagnosis_repair_tool_error(copilot_ctx, "edit_block_and_run", authority_error)

    _clear_pending_browser_interaction_observation(copilot_ctx)
    try:
        workflow_yaml = apply_block_edit(
            _stored_workflow_yaml(copilot_ctx),
            label,
            expected_code=expected_code,
            replacement_code=replacement_code,
        )
    except BlockEditError as exc:
        result = {"ok": False, "error": str(exc)}
        _carry_unresolved_failure_into_result(copilot_ctx, result, "edit_block_and_run")
        record_tool_step_result_for_ctx(copilot_ctx, "edit_block_and_run", arguments, result)
        finalize_build_test_result(
            copilot_ctx,
            source_tool="edit_block_and_run",
            result=result,
        )
        return json.dumps(sanitize_tool_result_for_llm("edit_block_and_run", result))

    prior_definition = await _get_prior_workflow_definition(copilot_ctx)
    with copilot_span("edit_block_and_run.update", data={"yaml_length": len(workflow_yaml)}):
        update_result = await _update_workflow(
            {"workflow_yaml": workflow_yaml, "_preserve_code_block_associations": True},
            copilot_ctx,
            allow_missing_credentials=skip_run_after_update,
            originating_call_id=_originating_call_id(ctx),
        )
        _record_workflow_update_result(copilot_ctx, update_result, prior_definition)
    if not update_result.get("ok"):
        _carry_unresolved_failure_into_result(copilot_ctx, update_result, "edit_block_and_run")
        record_tool_step_result_for_ctx(copilot_ctx, "edit_block_and_run", arguments, update_result)
        finalize_build_test_result(
            copilot_ctx,
            source_tool="edit_block_and_run",
            result=update_result,
        )
        return json.dumps(sanitize_tool_result_for_llm("update_workflow", update_result))

    if skip_run_after_update:
        return _credential_deferred_combined_tool_result(
            copilot_ctx,
            tool_name="edit_block_and_run",
            arguments=arguments,
            update_result=update_result,
        )

    return await _run_updated_workflow_blocks(
        copilot_ctx,
        tool_name="edit_block_and_run",
        arguments=arguments,
        update_result=update_result,
        prior_definition=prior_definition,
        block_labels=requested_labels,
        parameters=runtime_parameters,
        handler_start=handler_start,
    )


@function_tool(name_override="add_block", strict_mode=False)
async def add_block_tool(
    ctx: RunContextWrapper,
    after_label: str,
    block_yaml: str,
    parameters: list[dict[str, Any]] | None = None,
    code_artifact_metadata: list[CodeArtifactMetadata] | None = None,
    block_observation_refs: list[BlockObservationRef] | None = None,
) -> str:
    """Add one new block after an existing one, leaving every other block exactly as it is.

    Prefer this over update_and_run_blocks whenever you are adding to a workflow that already exists:
    you send only the new block, so the blocks that already work cannot be disturbed and the workflow
    is not retyped. `after_label` must name a block that exists; the new block is linked in directly
    after it and inherits what that block pointed at.

    Pass `block_yaml` as a single block mapping including its `label`. Declare any new top-level
    workflow parameters the block reads in `parameters` — a new block and the parameter it consumes
    have to land in the same call, or the workflow is briefly saved in a state that cannot run. For a
    code block pass its `code_artifact_metadata` row here too, since a brand-new block has none yet.

    Each workflow parameter is a flat object. For example:
    `{"parameter_type": "workflow", "key": "billing_history_path",
    "workflow_parameter_type": "string", "default_value": "BillingHistory.jsp"}`.
    Inspect or run the saved workflow to obtain a useful editable default when
    the page can reveal it; do not nest these fields under `workflow`.

    To change a block that already exists use edit_block; to remove one use delete_block.
    """
    copilot_ctx = ctx.context
    arguments = {"after_label": after_label, "parameters": parameters}
    authority_error = _authority_tool_error(copilot_ctx, "add_block")
    if authority_error:
        result = {"ok": False, "error": authority_error}
        finalize_build_test_result(
            copilot_ctx,
            source_tool="add_block",
            result=result,
            diagnosis_shadow_eligible=False,
        )
        return json.dumps(sanitize_tool_result_for_llm("add_block", result))
    try:
        workflow_yaml = add_block_to_workflow(
            _stored_workflow_yaml(copilot_ctx),
            after_label,
            block_yaml,
            parameters=parameters,
        )
    except BlockEditError as exc:
        result = {"ok": False, "error": str(exc)}
        record_tool_step_result_for_ctx(copilot_ctx, "add_block", arguments, result)
        finalize_build_test_result(
            copilot_ctx,
            source_tool="add_block",
            result=result,
            diagnosis_shadow_eligible=False,
        )
        return json.dumps(sanitize_tool_result_for_llm("add_block", result))
    return await _persist_block_scoped_edit(
        copilot_ctx,
        "add_block",
        workflow_yaml,
        arguments,
        code_artifact_metadata=code_artifact_metadata,
        block_observation_refs=block_observation_refs,
        originating_call_id=_originating_call_id(ctx),
    )


@function_tool(name_override="delete_block")
async def delete_block_tool(ctx: RunContextWrapper, label: str) -> str:
    """Remove one block from the workflow by label.

    Deleting is an explicit action: leaving a block out of a workflow you send elsewhere does not
    remove it. Any block that pointed at this one as its next step is unlinked.
    """
    copilot_ctx = ctx.context
    arguments = {"label": label}
    authority_error = _authority_tool_error(copilot_ctx, "delete_block")
    if authority_error:
        result = {"ok": False, "error": authority_error}
        finalize_build_test_result(
            copilot_ctx,
            source_tool="delete_block",
            result=result,
            diagnosis_shadow_eligible=False,
        )
        return json.dumps(sanitize_tool_result_for_llm("delete_block", result))
    try:
        workflow_yaml = delete_block_from_workflow(_stored_workflow_yaml(copilot_ctx), label)
    except BlockEditError as exc:
        result = {"ok": False, "error": str(exc)}
        record_tool_step_result_for_ctx(copilot_ctx, "delete_block", arguments, result)
        finalize_build_test_result(
            copilot_ctx,
            source_tool="delete_block",
            result=result,
            diagnosis_shadow_eligible=False,
        )
        return json.dumps(sanitize_tool_result_for_llm("delete_block", result))
    return await _persist_block_scoped_edit(
        copilot_ctx, "delete_block", workflow_yaml, arguments, originating_call_id=_originating_call_id(ctx)
    )


SUPERSEDED_BY_VALUE_WITNESS = "superseded-by-value-witness"


def _witnessed_output_paths(data: object) -> frozenset[str]:
    """Output paths this call's capture already addressed by the value its screenshot read."""
    if not isinstance(data, dict):
        return frozenset()
    witnesses = data.get("value_witnesses")
    if not isinstance(witnesses, list):
        return frozenset()
    return frozenset(
        str(witness["output_path"])
        for witness in witnesses
        if isinstance(witness, dict) and str(witness.get("output_path") or "")
    )


async def _verify_requested_output_reads(
    copilot_ctx: CopilotContext,
    reads: list[RequestedOutputRead],
    witnessed_paths: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Verify model-designated rendered values and return page facts without retaining a plan."""
    verified: list[dict[str, Any]] = []
    admitted, unverified = admitted_requested_output_reads(reads)
    for read in admitted:
        output_path = read.output_path
        value_text = read.value_text
        label = read.label
        if output_path in witnessed_paths:
            unverified.append({"output_path": output_path, "reason": SUPERSEDED_BY_VALUE_WITNESS})
            continue
        server = copilot_ctx.discovery_mcp_server
        if server is None:
            unverified.append({"output_path": output_path, "reason": "no-browser"})
            continue
        try:
            raw = await asyncio.wait_for(
                server.call_internal_tool(
                    "skyvern_evaluate",
                    {"expression": value_designation_probe_expression(value_text, label)},
                ),
                timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
            )
        except Exception:
            unverified.append({"output_path": output_path, "reason": "probe-failed"})
            continue
        payload = (raw.get("data") or {}).get("result") if isinstance(raw, dict) and raw.get("ok") else None
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = None
        if not isinstance(payload, dict) or payload.get("error") or not isinstance(payload.get("text"), str):
            reason = str(payload.get("error")) if isinstance(payload, dict) and payload.get("error") else "no-result"
            unverified.append({"output_path": output_path, "reason": reason})
            continue
        raw_candidates = payload.get("selector_candidates")
        candidates: list[dict[str, Any]] = []
        if isinstance(raw_candidates, list):
            for candidate in raw_candidates:
                if isinstance(candidate, str) and candidate:
                    candidates.append(
                        {"selector": candidate, "source": "unknown", "match_count": None, "position": None}
                    )
                elif isinstance(candidate, dict) and isinstance(candidate.get("selector"), str):
                    candidates.append(
                        {
                            "selector": candidate["selector"],
                            "source": str(candidate.get("source") or "unknown"),
                            "match_count": candidate.get("match_count"),
                            "position": candidate.get("position"),
                        }
                    )
        if not candidates:
            unverified.append({"output_path": output_path, "reason": "no-stable-selector"})
            continue
        label_association = str(payload.get("label_association") or "")
        fact = {
            "output_path": output_path,
            "label": label if label_association != "not_found" else "",
            "rendered_value": payload["text"],
            "selector_candidates": candidates,
            "page_url": str(payload.get("url") or ""),
        }
        if label_association:
            fact["label_association"] = label_association
        verified.append(fact)
    LOG.info(
        "copilot_requested_output_designation_facts",
        verified_paths=[fact["output_path"] for fact in verified],
        unverified=unverified,
    )
    return verified, unverified


@function_tool(name_override="list_credentials")
async def list_credentials_tool(
    ctx: RunContextWrapper,
    page: int = 1,
    page_size: int = 10,
    exact_reference: str | None = None,
) -> str:
    """List stored credentials (metadata only — never passwords or secrets).
    Use this to find credential IDs for login blocks.

    When the agent selects a saved name or credential ID that appears as a complete
    credential reference in the latest literal user turn, pass it as `exact_reference`.
    Exact mode verifies provenance and organization-wide cardinality, then atomically binds
    the single match into server-owned request authority. It does not classify the surrounding
    prose and never falls back to fuzzy search, discovery, or pagination. Zero or multiple exact
    matches grant no authority.

    Without `exact_reference`, this is metadata-only discovery. Paginated. `page_size` caps at 50. The response includes `has_more`;
    before concluding no credential exists, keep incrementing `page` until
    `has_more` is `false` — otherwise you risk telling the user to create
    a credential they have already stored on a later page.
    """
    copilot_ctx = ctx.context
    arguments = {"page": page, "page_size": page_size, "exact_reference": exact_reference}
    authority_error = _authority_tool_error(copilot_ctx, "list_credentials")
    if authority_error:
        result = {"ok": False, "error": authority_error}
        record_tool_step_result_for_ctx(copilot_ctx, "list_credentials", arguments, result)
        return json.dumps(result)

    result = await _list_credentials(arguments, copilot_ctx)
    record_tool_step_result_for_ctx(copilot_ctx, "list_credentials", arguments, result)
    sanitized = sanitize_tool_result_for_llm("list_credentials", result)
    return json.dumps(sanitized)


@function_tool(name_override="request_credential")
async def request_credential_tool(ctx: RunContextWrapper, login_page_url: str, reason: str) -> str:
    """Ask the user, in chat, to add or pick a saved credential for a sign-in page.

    Use this instead of writing a prose question when the turn needs a login and no saved
    credential covers that site. `login_page_url` must be a page on a site the user themselves
    provided in this chat; `reason` is one sentence shown to the user saying why the login is
    needed. The call waits for the user's answer and comes back `connected` with the credential
    to bind, `skipped`, `unanswered`, or `unavailable` — follow the `next` or `fallback` it
    carries. One ask per turn.
    """
    copilot_ctx = ctx.context
    arguments = {"login_page_url": login_page_url, "reason": reason}
    result: dict[str, Any] = {}
    try:
        authority_error = _authority_tool_error(copilot_ctx, "request_credential")
        if authority_error:
            result = {"ok": False, "error": authority_error}
        else:
            result = await _request_credential(login_page_url, reason, copilot_ctx)
    finally:
        # A card on screen right now owns the gate; this call must not open it for one it never
        # raised. Every other exit has to release, including a repeat ask in a later response.
        if not copilot_ctx.credential_ask_in_flight:
            release_credential_pause_gate(copilot_ctx)
    record_tool_step_result_for_ctx(copilot_ctx, "request_credential", arguments, result)
    return json.dumps(result)


@function_tool(name_override="ask_user")
async def ask_user_tool(ctx: ToolContext[CopilotContext], parts: list[QuestionInput]) -> str:
    """Ask the user questions in an inline card and receive their response before continuing.

    Keep questions and choice labels concise. Aim for 200 characters or fewer per question or choice, and offer no more than eight choices.

    Give each question a prompt and optional choices. The card always has a bottom free-text
    field. Users can submit choices, free text, or both, answer some parts, or skip. The result
    preserves the questions, chosen options, free text, and unanswered parts. Decide what to do
    next from those facts, including explaining or asking again. Use request_credential for
    credentials; an ordinary answer does not change existing action or credential permissions.
    """
    from skyvern.forge.sdk.copilot.ask_user import ask_user

    return json.dumps(await ask_user(ctx.context, AskUserArguments(parts=parts), ctx.tool_call_id))


@function_tool(name_override="list_integrations")
async def list_integrations_tool(ctx: RunContextWrapper) -> str:
    """List the organization's connected Google and Microsoft accounts (metadata only —
    never tokens). Each entry has `connection_id`, `provider`, `name`, `state`,
    `email_address`, and `scopes_granted`.

    These are OAuth connections made on the Integrations page, NOT the stored
    login credentials returned by `list_credentials` — the two lists are disjoint,
    so check this one before concluding the user has no Google or Microsoft access.
    Prefer a purpose-built native integration block over automating that connected
    service through its browser UI. For Google Sheets, call `get_block_schema` for
    `google_sheets_read` or `google_sheets_write`, then pass an active compatible
    connection's `connection_id` as the block's `credential_id`. Not paginated; one
    call returns every connection.

    Match on `scopes_granted`, not on `provider` alone: connections are granted per
    product, so a Sheets connection cannot read Gmail and binding it to a mail block
    fails at run time. A connection whose `state` is `active` can mint an access token.
    A connection whose `state` is `error` remains listed but cannot mint one until it
    is authorized again.

    When the current request identifies one active, scope-compatible row, bind its
    exact `connection_id` as the native block's `credential_id` and continue.
    For a build-and-test request, pass the completed workflow and the bound block
    label to `update_and_run_blocks` in the same turn; do not stop at
    `update_workflow`.
    Never require an opaque ID already present in this tool result to be repeated.
    If the requested account remains ambiguous or no compatible active row exists,
    use a grounded ordinary-language clarification instead.

    A `credential_id` may instead be the connection `name` or `email_address`
    exactly as the user wrote it in this turn. The server resolves it and reports
    the outcome under `google_connection_resolution` in the `update_workflow`
    result: `resolved` rewrites the slot to the `connection_id`, while `ambiguous`,
    `not_found`, `ineligible`, and `not_cited` leave it alone and return the
    eligible rows to clarify against. A slot still holding an unresolved reference
    cannot run.
    """
    copilot_ctx = ctx.context
    arguments: dict[str, Any] = {}
    authority_error = _authority_tool_error(copilot_ctx, "list_integrations")
    if authority_error:
        result = {"ok": False, "error": authority_error}
        record_tool_step_result_for_ctx(copilot_ctx, "list_integrations", arguments, result)
        return json.dumps(result)

    result = await _list_integrations(arguments, copilot_ctx)
    record_tool_step_result_for_ctx(copilot_ctx, "list_integrations", arguments, result)
    sanitized = sanitize_tool_result_for_llm("list_integrations", result)
    return json.dumps(sanitized)


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

    If an existing saved block can establish the state you need, run that
    block unchanged before scouting the resulting page. In particular, a saved
    login block can use its bound credential during the workflow run even when
    that credential is not authorized for direct debug-browser filling.

    For diagnostic complaints with no prior edit goal, inspect the current
    workflow and existing run evidence before deciding whether a fresh run is
    needed. If prior context establishes a resolvable edit, use
    `update_and_run_blocks` instead of rerunning unchanged blocks.

    Pass runtime values for workflow parameters via the `parameters` dict —
    keys must match the workflow parameter `key` field. When the user has
    supplied concrete non-secret values in their message (names, emails, IDs),
    pass them on the first call rather than letting the workflow fall back to
    placeholders. For sensitive values (password, secret, token, api_key,
    credential, totp, otp, one_time_code, private_key, auth) — call
    `list_credentials` and use a credential parameter whose default_value is
    the stored `credential_id`. If no stored credential matches, do NOT pass
    the inline value via `parameters`. Ask the user to store it as a saved
    credential and reply with the credential name; do not build or run with
    the raw value.

    Use browser inspection and run evidence to fill knowledge gaps before
    changing the workflow. If visible state is uncertain, inspect the live
    page and then compose the next normal workflow action from observed
    evidence instead of retrying guessed URL params or page structure.
    """
    copilot_ctx = ctx.context
    await await_pending_credential_pause(copilot_ctx)
    copilot_ctx.completion_verification_result = None
    handler_start = time.monotonic()
    arguments = {"block_labels": block_labels, "parameters": parameters or {}}
    authority_error = _authority_tool_error(copilot_ctx, "run_blocks_and_collect_debug")
    if authority_error:
        return _diagnosis_repair_tool_error(copilot_ctx, "run_blocks_and_collect_debug", authority_error)

    prior_definition = await _get_prior_workflow_definition(copilot_ctx)
    # No definition change on this path, so the frontier never reaches the edit-in-place branch
    # and a live page read would be spent on nothing.
    labels_to_execute, block_outputs_to_seed, frontier_start_label, start_provenance = _plan_frontier(
        copilot_ctx, block_labels, prior_definition, prior_definition
    )
    copilot_ctx.frontier_start_provenance = start_provenance
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
        with pending_operation("tool.run_blocks_and_collect_debug"):
            result = await _run_blocks_and_collect_debug(
                arguments,
                copilot_ctx,
                labels_to_execute=labels_to_execute,
                block_outputs_to_seed=block_outputs_to_seed,
                frontier_start_label=frontier_start_label,
            )
        recorded_outcome = await _verify_and_record_run_blocks_result(copilot_ctx, result, handler_start)
        _carry_unresolved_failure_into_result(copilot_ctx, result, "run_blocks_and_collect_debug")
        record_tool_step_result_for_ctx(copilot_ctx, "run_blocks_and_collect_debug", arguments, result)
        finalize_build_test_result(
            copilot_ctx,
            source_tool="run_blocks_and_collect_debug",
            result=result,
            recorded_outcome=recorded_outcome,
        )
        enqueue_screenshot_from_result(
            copilot_ctx,
            result,
            provenance=_run_result_screenshot_provenance(result, source_tool="run_blocks_and_collect_debug"),
        )

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
    authority_error = _authority_tool_error(copilot_ctx, "get_run_results")
    if authority_error:
        return json.dumps({"ok": False, "error": authority_error})
    result = await _get_run_results(params, copilot_ctx)
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
    This persists the workflow and remotely executes the selected frontier, waiting for it to
    finish, so it is materially higher latency than a bounded page read. It is the surface for
    testing durable behaviour, and for reaching a state that only execution can establish --
    authentication, a credential or OTP step, or state an upstream block creates.
    Use this instead of calling update_workflow and run_blocks_and_collect_debug separately.
    The workflow must validate successfully before blocks are run.

    `block_labels` may be a tested frontier subset of the full workflow YAML;
    save the complete reusable workflow, then run only the next 1-2 unverified
    blocks when a long form/search/result chain can be verified incrementally.

    Top-level workflow parameter keys appear in the run-input UI. When you
    add runtime inputs in `workflow_definition.parameters`, name keys for the
    reusable domain value the user supplies, not the page widget or action used
    to enter it.

    For diagnostic complaints with no prior edit goal, inspect the current
    workflow and run evidence first. A diagnostic follow-up after an explicit
    edit goal may update and run once the correction is clear.

    Pass runtime values for workflow parameters via the `parameters` dict —
    keys must match the workflow parameter `key` field. When the user has
    supplied concrete non-secret values in their message (names, emails, IDs),
    pass them on the first call rather than letting the workflow fall back to
    placeholders. For sensitive values (password, secret, token, api_key,
    credential, totp, otp, one_time_code, private_key, auth) — call
    `list_credentials` and use a credential parameter whose default_value is
    the stored `credential_id`. If no stored credential matches, do NOT pass
    the inline value via `parameters`. Ask the user to store it as a saved
    credential and reply with the credential name; do not build or run with
    the raw value.

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
    await await_pending_credential_pause(copilot_ctx)
    copilot_ctx.completion_verification_result = None
    handler_start = time.monotonic()
    serialized_code_artifact_metadata: object = _code_artifact_metadata_as_tool_argument(code_artifact_metadata)
    normalized_block_observation_refs = normalize_block_observation_refs(block_observation_refs)
    arguments = {
        "workflow_yaml": workflow_yaml,
        "block_labels": block_labels,
        "block_observation_refs": normalized_block_observation_refs,
        "code_artifact_metadata": serialized_code_artifact_metadata,
        "parameters": parameters or {},
    }
    skip_run_after_update = _update_and_run_requires_skipped_run(copilot_ctx, "update_and_run_blocks")
    # Cleared unconditionally up front and only set True at the actual skip
    # branch below — reflects "we skipped a run", not "the policy would have
    # allowed a skip if we got that far". A stale True from an earlier call, or
    # a premature True from a policy check ahead of an unrelated update_workflow
    # failure, would misreport an authoring error as a credential ask.
    copilot_ctx.last_run_skipped_unbound_credentials = False
    authority_error = _authority_tool_error(copilot_ctx, "update_and_run_blocks")
    if authority_error:
        return _diagnosis_repair_tool_error(copilot_ctx, "update_and_run_blocks", authority_error)

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
                "block_labels": block_labels,
                "parameters": parameters or {},
            },
            copilot_ctx,
            allow_missing_credentials=skip_run_after_update,
            originating_call_id=_originating_call_id(ctx),
        )
        _record_workflow_update_result(copilot_ctx, update_result, prior_definition)

    if not update_result.get("ok"):
        _carry_unresolved_failure_into_result(copilot_ctx, update_result, "update_and_run_blocks")
        record_tool_step_result_for_ctx(copilot_ctx, "update_and_run_blocks", arguments, update_result)
        finalize_build_test_result(
            copilot_ctx,
            source_tool="update_and_run_blocks",
            result=update_result,
        )
        sanitized = sanitize_tool_result_for_llm("update_workflow", update_result)
        return json.dumps(sanitized)

    if skip_run_after_update:
        return _credential_deferred_combined_tool_result(
            copilot_ctx,
            tool_name="update_and_run_blocks",
            arguments=arguments,
            update_result=update_result,
        )

    return await _run_updated_workflow_blocks(
        copilot_ctx,
        tool_name="update_and_run_blocks",
        arguments=arguments,
        update_result=update_result,
        prior_definition=prior_definition,
        block_labels=block_labels,
        parameters=parameters or {},
        handler_start=handler_start,
    )


def _credential_deferred_combined_tool_result(
    copilot_ctx: CopilotContext,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    update_result: dict[str, Any],
) -> str:
    """Record the staged draft when a combined edit/update cannot safely run yet."""
    copilot_ctx.last_run_skipped_unbound_credentials = True
    skip_result = {
        "ok": True,
        "message": "Skipped test run: required credentials are not configured.",
        "data": {
            "block_count": copilot_ctx.last_update_block_count,
            "workflow_updated": True,
            "skipped_run": True,
            "skip_reason": "workflow_credential_inputs_unbound",
        },
    }
    skip_result = carry_author_time_findings(update_result, skip_result)
    record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, skip_result)
    finalize_build_test_result(
        copilot_ctx,
        source_tool=tool_name,
        result=skip_result,
        workflow_updated=True,
    )
    LOG.info(
        "combined workflow tool skipped run on unbound credential workflow inputs",
        tool_name=tool_name,
        workflow_permanent_id=copilot_ctx.workflow_permanent_id,
    )
    return json.dumps(sanitize_tool_result_for_llm(tool_name, skip_result))


async def _run_updated_workflow_blocks(
    copilot_ctx: CopilotContext,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    update_result: dict[str, Any],
    prior_definition: object | None,
    block_labels: list[str],
    parameters: dict[str, Any],
    handler_start: float,
) -> str:
    """Run a just-persisted definition through the shared frontier and debug-evidence seam."""
    new_definition = None
    if copilot_ctx.last_workflow is not None:
        new_definition = getattr(copilot_ctx.last_workflow, "workflow_definition", None)

    labels_to_execute, block_outputs_to_seed, frontier_start_label, start_provenance = _plan_frontier(
        copilot_ctx,
        block_labels,
        prior_definition,
        new_definition,
        await _frontier_runtime_page_url(copilot_ctx),
    )
    copilot_ctx.frontier_start_provenance = start_provenance
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
        with pending_operation("tool.update_and_run_blocks"):
            run_result = await _run_blocks_and_collect_debug(
                {"block_labels": block_labels, "parameters": parameters},
                copilot_ctx,
                labels_to_execute=labels_to_execute,
                block_outputs_to_seed=block_outputs_to_seed,
                frontier_start_label=frontier_start_label,
            )
        recorded_outcome = await _verify_and_record_run_blocks_result(copilot_ctx, run_result, handler_start)
        run_result = carry_author_time_findings(update_result, run_result)
        _carry_unresolved_failure_into_result(copilot_ctx, run_result, tool_name)
        record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, run_result)
        finalize_build_test_result(
            copilot_ctx,
            source_tool=tool_name,
            result=run_result,
            workflow_updated=True,
            recorded_outcome=recorded_outcome,
        )
        enqueue_screenshot_from_result(
            copilot_ctx,
            run_result,
            provenance=_run_result_screenshot_provenance(run_result, source_tool=tool_name),
        )
    sanitized = sanitize_tool_result_for_llm("run_blocks_and_collect_debug", run_result)
    return json.dumps(sanitized)


def _run_result_screenshot_provenance(result: dict[str, Any], *, source_tool: str) -> ScreenshotProvenance:
    """Bind a run-result frame to the facts carried by the run payload.

    Run execution returns its identity fields under ``data``. Some older call
    sites and error paths still expose them at the top level, so retain that as
    a factual fallback rather than stamping the frame as unidentified.
    """
    data = result.get("data")
    facts = data if isinstance(data, dict) else {}

    def _string_fact(key: str) -> str | None:
        value = facts.get(key)
        if not isinstance(value, str) or not value:
            value = result.get(key)
        return value if isinstance(value, str) and value else None

    return ScreenshotProvenance(
        source_tool=source_tool,
        captured_url=_string_fact("current_url"),
        observation_step=None,
        browser_session_id=_string_fact("browser_session_id"),
        workflow_run_id=_string_fact("workflow_run_id"),
        action_relation=ScreenshotActionRelation.WORKFLOW_RUN_RESULT,
    )


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
    domain (``example.com``), or a single brand word. A brand word is resolved
    only from an exact provider-backed official-site association that safely
    navigates over public-network HTTPS to the associated origin. Results use
    ``contract_version=discover_workflow_entrypoint_v3``. English phrases
    ("the X website") return
    ``failure_reason=could_not_resolve_site_name`` — ASK_QUESTION for a URL.

    Returns ``candidate_url`` plus a short ``evidence_trail`` and any
    ``candidate_form_fields``. Evidence-backed brand results also include
    bounded ``candidate_provenance`` and ``navigation_evidence``. Use
    ``candidate_url`` as the ``url`` value on a ``goto_url`` block. Do NOT
    paste the evidence into workflow YAML.

    Discovery navigates and reads pages; it will NOT type, click form buttons,
    run JavaScript, or submit forms.
    """
    authority_error = _authority_tool_error(ctx.context, "discover_workflow_entrypoint")
    if authority_error:
        return _diagnosis_repair_tool_error(ctx.context, "discover_workflow_entrypoint", authority_error)
    result = await _discover_workflow_entrypoint_impl(ctx.context, site_or_url, intent_hint)
    return json.dumps(scrub_secrets_from_structure(ctx.context, result))


@function_tool(name_override="inspect_page_for_composition", strict_mode=False)
async def inspect_page_for_composition_tool(
    ctx: RunContextWrapper,
    target_url: str,
    requested_output_reads: list[RequestedOutputRead] | None = None,
) -> str:
    """Inspect a known page before composing form/search workflow blocks.

    This is a bounded read of known or current page state: it is the surface for uncertainty about
    controls, selectors, visible state or layout, where no workflow execution is required.
    Use this after the entrypoint URL is known and before authoring blocks that
    fill fields, submit searches, filter results, or expand result rows. It
    can also inspect the current browser page after a run by passing
    target_url="current_page"; use that after partial/budgeted runs so you do
    not replay a search that already advanced the page. Passing any other
    `target_url` navigates the live browser there and reports the reached
    `current_url`, so a further `navigate_browser` to that same URL is redundant. The packet
    describes the page only as it is at that moment: a control that appears solely after an
    interaction -- a Delete control after an Add click, a cart after add-to-cart, the secure area
    after login -- is absent from it until that interaction has happened.

    Returns observed page evidence: current URL, title, navigation targets, form
    fields with labels and selectors, submit/search controls, result containers,
    compact visible text excerpts, anti-bot indicators, and bounded visual
    challenge evidence when DOM evidence shows challenge state. The returned
    `observation_step` is the side-channel id to pass in `block_observation_refs`
    when a newly authored block acts on this observed page. Do NOT paste the
    evidence into workflow YAML; use it to ground concise block prompts. If a
    select reports `options_omitted=true`, `option_count` is the observed total and
    its selector remains available. If those options are needed, use one existing
    `evaluate` call to read only that select; do not repeat the full-page inspection.
    If a block run changes pages, inspect the reached page before authoring downstream
    form/search/result blocks. If the evidence shows required fields or controls that
    the user did not supply enough information for, ASK_QUESTION with that observed missing input. If
    evidence is sufficient, compose and run workflow blocks from the observed fields.
    `challenge_state` reports what the page looks like, which is not what a run will do:
    it does not establish that a submit/search path is closed, and a run settles that.

    When the page visibly shows a requested output but its markup is unclear, pass
    `requested_output_reads` with the `output_path` your block will return, the exact
    rendered `value_text`, and its visible `label`. The browser verifies the designation
    and returns every observed selector candidate with its cardinality as facts; you
    remain responsible for choosing a selector and authoring the workflow read.
    """
    authority_error = _authority_tool_error(ctx.context, "inspect_page_for_composition")
    if authority_error:
        return _diagnosis_repair_tool_error(ctx.context, "inspect_page_for_composition", authority_error)
    admitted, _ = admitted_requested_output_reads(requested_output_reads or [])
    result = await _inspect_page_for_composition_impl(ctx.context, target_url, admitted)
    if requested_output_reads and result.get("ok"):
        data = result.get("data")
        witnessed_paths = _witnessed_output_paths(data)
        verified, unverified = await _verify_requested_output_reads(
            ctx.context, requested_output_reads, witnessed_paths
        )
        if isinstance(data, dict):
            data["requested_output_designations"] = verified
            if unverified:
                data["unverified_output_designations"] = unverified
                retry_paths = [
                    item["output_path"]
                    for item in unverified
                    if item.get("output_path") and item.get("reason") != SUPERSEDED_BY_VALUE_WITNESS
                ]
                if retry_paths:
                    data["requested_output_designation_capability"] = requested_output_designation_capability(
                        retry_paths
                    )
    elif result.get("ok"):
        requested_paths = requested_output_paths_for_derivation(ctx.context)
        data = result.get("data")
        if requested_paths and isinstance(data, dict):
            data["requested_output_designation_capability"] = requested_output_designation_capability(
                list(requested_paths)
            )
    scrubbed_result = scrub_secrets_from_structure(ctx.context, result)
    return json.dumps(_model_facing_inspect_result(scrubbed_result))


@function_tool(name_override="fill_credential_field", strict_mode=False)
async def fill_credential_field_tool(
    ctx: RunContextWrapper,
    selector: str,
    credential_id: str,
    field: str,
    submit_selector: str | None = None,
) -> str:
    """Fill ONE field of a SAVED credential into the live debug browser during code-only scouting.

    The secret value is resolved server-side from the stored credential and never
    enters the conversation; the result reports only `typed_length`. Use this
    instead of `type_text` whenever a login form field should receive a saved
    credential's username, password, or authenticator-app one-time code. Email/SMS
    OTP credentials are not filled during scouting because scouting has no
    workflow run/task context for safe polling.

    The result's `readback_outcome` reports what the field held right after the fill —
    `exact_match`, `different` (the field holds something other than what was typed),
    `empty`, or `unavailable` (the field could not be read) — and only `empty` fails
    the fill; decide from that outcome whether the page still needs another action.
    An `empty` readback still succeeds when `landing_inferred_from_navigation` is true:
    the page left the one the fill acted on, so the field was cleared by its own submit.

    Do not use this tool to reproduce an existing saved login block. Run that
    block unchanged with `run_blocks_and_collect_debug`, then inspect the page
    it reaches; the block's bound credential resolves in the workflow run.

    `selector` must be a CSS selector for the exact input field (no comma-union
    fallbacks — inspect the page first and target the proven field).
    `credential_id`: when a page observation returns
    `resolved_login_credential_id`, the server has already authorized that
    credential for this login page — pass that id. When it returns
    `candidate_login_credentials`, ask the user which one to use and pass the
    `credential_id` they choose. `field` is one of `username`, `password`, `totp`.

    `submit_selector` is optional and submits in the SAME call: pass the CSS
    selector of the form's submit control and this tool clicks it once the fill
    has been typed, including when the field could not be read back. The one case
    it does not click is a `totp` field that reads back holding something else,
    because submitting a code the field does not hold voids it. The
    result's `submitted` says outright whether the control was clicked; when it is
    false, `submit_skipped` or `submit_error` says why, and the code is still
    waiting to be submitted. A selector matching more than one control is not
    clicked at all rather than guessed between. A one-time code expires in
    seconds, so for `field="totp"` always
    inspect the page for the submit control FIRST and pass its selector here —
    submitting on a later turn can send an already-expired code. Take that
    selector from `inspect_page_for_composition`, which you have already run on
    the sign-in page; this tool's own `form_submit_controls` is reported only when
    nothing was submitted, so it cannot supply the selector for the same call. Omit `submit_selector` and the
    tool fills only; it never clicks on its own. Each successful fill is recorded
    as a scouted interaction with the credential identity and field, and an
    in-call submit is recorded as the click that followed it.

    In model-authored code blocks, one-time codes resolve via two paths:
    **credential-bound:** `await <parameter_key>.otp()` for a saved credential whose
    totp_type is authenticator, email, or text — sources are specified at credential
    creation. **identifier-based:** `await otp("<address>")` for passwordless email-code
    sign-in, where the code lands in a connected Gmail or Outlook inbox and address is
    a bare email (no saved credential required). For the credential-bound path, choose and
    declare the workflow parameter key, then cite the observed `credential_id` and
    `credential_field` in `code_artifact_metadata.input_bindings`; use that declared
    parameter in the authored code. For the identifier-based path, pass only the email
    address string to `otp()` and ensure an active Gmail or Outlook connection exists
    for that mailbox.
    """
    result = await _fill_credential_field_impl(ctx.context, selector, credential_id, field, submit_selector)
    return json.dumps(scrub_secrets_from_structure(ctx.context, result))


async def _inspect_locator_matches_invoke(ctx: RunContextWrapper, arguments: str) -> str:
    """Serve one locator inspection against the browser the call named.

    The advertised contract is the tested one: ``TOOL_DESCRIPTION`` and ``TOOL_SCHEMA`` are the
    constants the ablation ran against, not a docstring or a schema derived from this signature.
    """
    copilot_ctx = ctx.context

    def finish(result: dict[str, Any]) -> str:
        # This tool reads Playwright directly and never crosses the MCP adapter; the exact-value
        # scrubber carries each secret's encoded forms, so a credential echoed URL-encoded in the URL
        # or HTML-escaped in outer HTML is caught here too. Scrub once, then record and return that
        # same structure, so nothing downstream retains an unsanitized copy of page text.
        scrubbed = scrub_secrets_from_structure(copilot_ctx, result)
        record_tool_step_result_for_ctx(copilot_ctx, LOCATOR_INSPECTION_TOOL_NAME, parsed, scrubbed)
        return json.dumps(scrubbed)

    try:
        parsed = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    target = parsed.get("target")
    raw_selectors = parsed.get("selectors")

    authority_error = _authority_tool_error(copilot_ctx, LOCATOR_INSPECTION_TOOL_NAME)
    if authority_error:
        return _diagnosis_repair_tool_error(copilot_ctx, LOCATOR_INSPECTION_TOOL_NAME, authority_error)

    binding = resolve_browser_session_binding(copilot_ctx, {"target": target})
    if binding.unavailable_reason:
        # Never fall back to the chat's browser: a locator inspected there answers a different
        # question than the one the model asked.
        return finish({"ok": False, "error": binding.unavailable_reason, **binding.provenance()})

    selectors = [s for s in (raw_selectors or []) if isinstance(s, str) and s.strip()]
    if not selectors:
        return finish({"ok": False, "error": "No selectors supplied.", **binding.provenance()})

    with bound_call_browser_session(binding.session_id_override):
        run_id = copilot_ctx.last_run_blocks_workflow_run_id
        if sensitive_origin_page_facts_withheld(copilot_ctx, run_id):
            return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR, **binding.provenance()})
        browser_state = await resolve_browser_state_for_context(copilot_ctx)
        if browser_state is None:
            return finish(
                {
                    "ok": False,
                    "error": "That browser is no longer available, so its page cannot be inspected.",
                    **binding.provenance(),
                }
            )
        page = await browser_state.get_working_page()
        if page is None:
            # Creating one would both mutate browser state and report from a page the run never
            # reached, which is the opposite of what this tool is for.
            return finish(
                {
                    "ok": False,
                    "error": "That browser has no open page to inspect.",
                    **binding.provenance(),
                }
            )
        facts = await inspect_locator_matches(page, selectors)
        if sensitive_origin_page_facts_withheld(copilot_ctx, run_id):
            return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR, **binding.provenance()})
        return finish({"ok": True, "current_url": page.url, **binding.provenance(), "data": facts})


inspect_locator_matches_tool = FunctionTool(
    name=LOCATOR_INSPECTION_TOOL_NAME,
    description=LOCATOR_INSPECTION_TOOL_DESCRIPTION,
    params_json_schema=LOCATOR_INSPECTION_TOOL_SCHEMA,
    on_invoke_tool=_inspect_locator_matches_invoke,
    strict_json_schema=False,
)


NATIVE_TOOLS = [
    ask_user_tool,
    update_workflow_tool,
    edit_block_tool,
    edit_block_and_run_tool,
    add_block_tool,
    delete_block_tool,
    list_credentials_tool,
    list_integrations_tool,
    run_blocks_tool,
    get_run_results_tool,
    update_and_run_blocks_tool,
    discover_workflow_entrypoint_tool,
    inspect_page_for_composition_tool,
    inspect_locator_matches_tool,
    fill_credential_field_tool,
    request_credential_tool,
]

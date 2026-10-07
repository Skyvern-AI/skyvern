"""Copilot agent tools — native handlers, hooks, and registration."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import time
from contextlib import nullcontext
from dataclasses import asdict
from typing import Any

import structlog
from agents import FunctionTool, function_tool
from agents.run_context import RunContextWrapper
from agents.tool_context import ToolContext
from pydantic import JsonValue

from skyvern.forge import app as app
from skyvern.forge.sdk.copilot.ask_user import (
    ACCOUNT_GROUP_CANCEL_TOOL_NAME,
    ACCOUNT_GROUP_STATUS_TOOL_NAME,
    ACCOUNT_GROUP_SUBMIT_TOOL_NAME,
    CREDENTIAL_DELETE_TOOL_NAME,
    AskUserArguments,
    QuestionInput,
)
from skyvern.forge.sdk.copilot.browser_target import (
    BROWSER_TARGET_PARAM_NAME,
    BrowserTarget,
    resolve_browser_session_binding,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    composition_page_evidence_missing as composition_page_evidence_missing,
)
from skyvern.forge.sdk.copilot.composition_evidence import has_bounded_page_schema as has_bounded_page_schema
from skyvern.forge.sdk.copilot.composition_evidence import (
    normalize_block_observation_refs,
)
from skyvern.forge.sdk.copilot.composition_evidence import workflow_target_url as workflow_target_url
from skyvern.forge.sdk.copilot.config import AuthoringCapability, BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import USER_FACING_REASON_PARAM, USER_FACING_REASON_SCHEMA, CopilotContext
from skyvern.forge.sdk.copilot.credential_pause import (
    await_pending_credential_pause,
    credential_pause_transport_ready,
    raw_secret_card_origin,
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
    browser_page_custody_lock,
    browser_session_recovery,
    resolve_browser_state_for_context,
    sensitive_origin_page_facts_withheld,
)
from skyvern.forge.sdk.copilot.screenshot_utils import (
    ScreenshotActionRelation,
    ScreenshotProvenance,
    capturing_tool_call,
    enqueue_screenshot_from_result,
)
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure
from skyvern.forge.sdk.copilot.tools.account_groups import (
    SUBMIT_TOOL_DESCRIPTION as ACCOUNT_GROUP_SUBMIT_TOOL_DESCRIPTION,
)
from skyvern.forge.sdk.copilot.tools.account_groups import (
    account_group_status,
    account_group_submit_enabled,
    cancel_account_group,
    run_for_accounts,
)
from skyvern.forge.sdk.copilot.tools.credential_deletion import (
    DELETE_TOOL_DESCRIPTION as CREDENTIAL_DELETE_TOOL_DESCRIPTION,
)
from skyvern.forge.sdk.copilot.tools.credential_deletion import (
    credential_delete_enabled,
    delete_saved_credentials,
)
from skyvern.forge.sdk.copilot.tools.locator_inspection import TOOL_DESCRIPTION as LOCATOR_INSPECTION_TOOL_DESCRIPTION
from skyvern.forge.sdk.copilot.tools.locator_inspection import TOOL_NAME as LOCATOR_INSPECTION_TOOL_NAME
from skyvern.forge.sdk.copilot.tools.locator_inspection import TOOL_SCHEMA as LOCATOR_INSPECTION_TOOL_SCHEMA
from skyvern.forge.sdk.copilot.tools.locator_inspection import (
    inspect_locator_matches,
)
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.work_plan import WorkPlanArguments, set_work_plan
from skyvern.forge.sdk.copilot.workflow_yaml import (
    BlockEditError,
)
from skyvern.forge.sdk.copilot.workflow_yaml import _process_workflow_yaml as _process_workflow_yaml
from skyvern.forge.sdk.copilot.workflow_yaml import (
    add_block_to_workflow,
    apply_block_edit,
    delete_block_from_workflow,
    preserve_untouched_block_configuration,
    stored_block_code,
    stored_workflow_yaml,
)
from skyvern.forge.sdk.schemas.workflow_copilot import CredentialRegistration
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition
from skyvern.utils.yaml_loader import dump_workflow_yaml

from ._shared import _COMPOSITION_STRIPPED_HTML_MAX_CHARS as _COMPOSITION_STRIPPED_HTML_MAX_CHARS
from ._shared import _DISCOVERY_PER_CALL_TIMEOUT_SECONDS as _DISCOVERY_PER_CALL_TIMEOUT_SECONDS
from ._shared import _FAILED_BLOCK_STATUSES as _FAILED_BLOCK_STATUSES
from ._shared import BLOCK_RUNNING_TOOLS as BLOCK_RUNNING_TOOLS
from ._shared import EDIT_BLOCK_AND_RUN_TOOL_NAME, EDIT_BLOCK_TOOL_NAME
from ._shared import RUN_BLOCKS_SAFETY_CEILING_SECONDS as RUN_BLOCKS_SAFETY_CEILING_SECONDS
from ._shared import UPDATE_AND_RUN_BLOCKS_TOOL_NAME, UPDATE_WORKFLOW_TOOL_NAME
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
from .attached_file_upload import UPLOAD_TOOL_NAME, upload_attached_file
from .banned_blocks import _COPILOT_BANNED_BLOCK_TYPES as _COPILOT_BANNED_BLOCK_TYPES
from .banned_blocks import AUTHORING_FAMILY_GUIDANCE as AUTHORING_FAMILY_GUIDANCE
from .banned_blocks import SCHEMA_FIRST_GUIDANCE as SCHEMA_FIRST_GUIDANCE
from .banned_blocks import _record_banned_block_reject_span as _record_banned_block_reject_span
from .banned_blocks import reject_authoring_violations as reject_authoring_violations
from .blockers import _analyze_run_blocks as _analyze_run_blocks
from .blockers import _run_blocks_structured_blocker_message as _run_blocks_structured_blocker_message
from .blockers import _trusted_post_drain_status as _trusted_post_drain_status
from .browser_code import TOOL_NAME as BROWSER_CODE_TOOL_NAME
from .browser_code import (
    resolve_executed_browser_code_source,
    run_browser_code_tool,
)
from .browser_session_extension import TOOL_DESCRIPTION as SESSION_EXTENSION_TOOL_DESCRIPTION
from .browser_session_extension import TOOL_NAME as SESSION_EXTENSION_TOOL_NAME
from .browser_session_extension import extend_browser_session
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
from .discovery import _resolve_discovery_entry_url as _resolve_discovery_entry_url
from .errors import copilot_tool_failure
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
    _read_google_sheet,
)
from .mcp_hooks import _build_skyvern_mcp_overlays as _build_skyvern_mcp_overlays
from .mcp_hooks import _click_post_hook as _click_post_hook
from .mcp_hooks import _click_pre_hook as _click_pre_hook
from .mcp_hooks import _evaluate_post_hook as _evaluate_post_hook
from .mcp_hooks import _get_block_schema_post_hook as _get_block_schema_post_hook
from .mcp_hooks import _get_block_schema_pre_hook as _get_block_schema_pre_hook
from .mcp_hooks import _navigate_post_hook as _navigate_post_hook
from .mcp_hooks import _normalized_authoring_capability as _normalized_authoring_capability
from .mcp_hooks import _press_key_post_hook as _press_key_post_hook
from .mcp_hooks import _screenshot_post_hook as _screenshot_post_hook
from .mcp_hooks import _select_option_post_hook as _select_option_post_hook
from .mcp_hooks import _type_text_post_hook as _type_text_post_hook
from .mcp_hooks import _verify_scout_type_landed as _verify_scout_type_landed
from .mcp_hooks import get_skyvern_mcp_alias_map as get_skyvern_mcp_alias_map
from .page_challenge import FRESH_BROWSER_TOOL_NAME, SOLVE_TOOL_NAME, solve_page_challenge, start_fresh_browser
from .page_observation import _record_composition_page_observation as _record_composition_page_observation
from .page_observation import _resolve_url_title as _resolve_url_title
from .run_execution import (
    RUN_RESULTS_MAX_ROW_KEYS,
)
from .run_execution import WatchdogExitReason as WatchdogExitReason
from .run_execution import _attach_action_traces as _attach_action_traces
from .run_execution import _block_end_urls_by_label as _block_end_urls_by_label
from .run_execution import _cancel_run_task_if_not_final as _cancel_run_task_if_not_final
from .run_execution import (
    _carry_unresolved_failure_into_result,
    _chronological_run_block_rows,
)
from .run_execution import _composition_anti_bot_reason as _composition_anti_bot_reason
from .run_execution import _detect_non_retriable_nav_error as _detect_non_retriable_nav_error
from .run_execution import (
    _diagnosis_repair_tool_error,
    _get_run_results,
)
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
    parse_run_results_cursor,
    project_run_results_page,
    run_block_loop_facts,
    run_workflow_end_to_end,
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
from .scouting import read_page_state as read_page_state
from .web_search import _search_web_impl as _search_web_impl
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
_REDACTED_SECRET_DEFERRED_DRAFT_MESSAGE = (
    "Saved this as a draft without running it: this turn contains a redacted secret, so its credential "
    "parameter is unbound and nothing runs. The in-chat credential card is available: call "
    "`request_credential` with the user's sign-in URL{site_hint} so they can connect a saved credential."
)


def _credential_deferred_draft_message(copilot_ctx: CopilotContext) -> str:
    """The Credentials-UI direction is the fallback for when the in-chat card cannot be shown."""
    policy = copilot_ctx.request_policy
    if policy is None or not policy.raw_secret_redacted_draft:
        return _CREDENTIAL_DEFERRED_DRAFT_MESSAGE
    if not credential_pause_transport_ready(copilot_ctx, copilot_ctx.copilot_config):
        return _CREDENTIAL_DEFERRED_DRAFT_MESSAGE
    origins = {raw_secret_card_origin(url) for url in policy.user_provided_site_urls} - {""}
    site_hint = f" ({', '.join(sorted(origins))})" if origins else "; ask the user for it if they gave none"
    return _REDACTED_SECRET_DEFERRED_DRAFT_MESSAGE.format(site_hint=site_hint)


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
    data["message"] = _credential_deferred_draft_message(copilot_ctx)


@function_tool(
    failure_error_function=copilot_tool_failure,
    name_override="update_workflow",
    strict_mode=False,
    tool_input_guardrails=[_WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL],
)
async def update_workflow_tool(
    ctx: RunContextWrapper,
    workflow: dict[str, Any],
    block_observation_refs: list[BlockObservationRef] | None = None,
    code_artifact_metadata: list[CodeArtifactMetadata] | None = None,
) -> str:
    """Validate and update the workflow definition.
    Provide the complete workflow as a `workflow` object with the same keys as the workflow YAML, e.g.
    `{"title": "Order lookup", "workflow_definition": {"parameters": [], "blocks": [{"block_type": "code",
    "label": "read_total", "code": "line one\\nline two"}]}}`. String values, including multiline code, are
    plain JSON strings.
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
    `observation_step` the page observation returned for the page that block
    acts on.
    For authored code blocks, include `code_artifact_metadata` rows describing
    declared goals, claimed outcomes, page dependencies, criteria, evidence
    refs, observation refs, and terminal verifier expectations.
    """
    workflow_yaml = dump_workflow_yaml(workflow)
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
    rebuilt_block_labels: list[str] | None = None,
    edited_label: str | None = None,
) -> str:
    """Send a server-composed workflow through the normal persistence path.

    The model never sends the whole workflow, but the saved result still goes through every
    author-time check, so a block edit cannot slip past what a full submission must satisfy.
    """
    prior_definition = await _get_prior_workflow_definition(copilot_ctx)
    params: dict[str, Any] = {"workflow_yaml": workflow_yaml, "_preserve_code_block_associations": True}
    authoring_prior_yaml = None
    if edited_label is not None:
        authoring_prior_yaml = preserve_untouched_block_configuration(
            _stored_workflow_yaml(copilot_ctx), prior_definition, edited_label=edited_label
        )
        params["workflow_yaml"] = preserve_untouched_block_configuration(
            workflow_yaml, prior_definition, edited_label=edited_label
        )
    if code_artifact_metadata is not None:
        params["code_artifact_metadata"] = _code_artifact_metadata_as_tool_argument(code_artifact_metadata)
        params["raw_code_artifact_metadata"] = code_artifact_metadata
    if block_observation_refs is not None:
        params["block_observation_refs"] = normalize_block_observation_refs(block_observation_refs)
    if rebuilt_block_labels:
        params["_rebuilt_block_labels"] = rebuilt_block_labels
    with copilot_span(tool_name, data={"yaml_length": len(workflow_yaml)}):
        result = await _update_workflow(
            params,
            copilot_ctx,
            originating_call_id=originating_call_id,
            block_scoped_authoring_prior_yaml=authoring_prior_yaml,
        )
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


@function_tool(failure_error_function=copilot_tool_failure, name_override=EDIT_BLOCK_TOOL_NAME, strict_mode=False)
async def edit_block_tool(
    ctx: RunContextWrapper,
    label: str,
    expected_code: str | None = None,
    replacement_code: str | None = None,
    fields: dict[str, Any] | None = None,
) -> str:
    """Change one block, leaving every other block exactly as it is.

    Prefer this over resending the whole workflow whenever you are changing an existing block: you send
    only the change, so a block that already works cannot be disturbed and the workflow is not retyped.

    For a `code` block, pass `expected_code` (a snippet of its current code, unique within that block)
    and `replacement_code`. The edit is rejected if the snippet is missing or appears more than once,
    which is how an edit written against a stale copy of the block fails instead of overwriting newer
    code. Read the block first if you are unsure what it currently contains. The snippet is matched
    against `data.stored_code[label]` from the latest write result, which can differ from what you
    submitted — `data.stored_code_rewritten` names the labels the server rewrote and
    `data.stored_code_withheld` the labels too large to return.

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
        copilot_ctx,
        "edit_block",
        workflow_yaml,
        arguments,
        originating_call_id=_originating_call_id(ctx),
        rebuilt_block_labels=[label] if replacement_code is not None or "code" in (fields or {}) else None,
        edited_label=label,
    )


@function_tool(
    failure_error_function=copilot_tool_failure,
    name_override=EDIT_BLOCK_AND_RUN_TOOL_NAME,
    timeout=RUN_BLOCKS_SAFETY_CEILING_SECONDS,
    strict_mode=False,
)
async def edit_block_and_run_tool(
    ctx: RunContextWrapper,
    label: str,
    expected_code: str | None = None,
    replacement_code: str | None = None,
    executed_source_reference: str | None = None,
    block_labels: list[str] | None = None,
    parameters: dict[str, Any] | None = None,
) -> str:
    """Apply one exact code edit and immediately test the affected frontier.

    Use this for a repair to an existing code block. ``label`` must name exactly one existing block,
    and ``expected_code`` must occur exactly once in its current stored code — that is
    ``data.stored_code[label]`` from the latest write result, which can differ from what you submitted
    (``data.stored_code_rewritten`` names the labels the server rewrote and ``data.stored_code_withheld``
    the labels too large to return). The tool changes only that code span, persists the reversible
    draft through the normal author-time safety boundary, then runs ``block_labels`` (or just
    ``label`` when omitted).

    This is one model-invoked edit and one run. It does not choose an edit, create a block, retry, or
    decide whether the result achieved the user's goal. Its response is the same sanitized run/debug
    evidence returned by ``run_blocks_and_collect_debug``; a failed run still leaves the edited draft
    and recorded workflow run available for the next turn.

    Pass current non-secret values for runtime workflow parameters in ``parameters``. For a raw
    secret value (for example, a password), do NOT pass an inline value. Ask the user to store it as a saved
    credential and reply with the credential name; do not build or run with the raw value.
    """
    copilot_ctx = ctx.context
    await await_pending_credential_pause(copilot_ctx)
    copilot_ctx.completion_verification_result = None
    # Cleared before any validation return, as update_and_run_blocks does, so a credential skip left by an
    # earlier call is not reported as the reason this edit failed.
    copilot_ctx.last_run_skipped_unbound_credentials = False
    requested_labels = list(block_labels) if block_labels else [label]
    runtime_parameters = parameters or {}
    reference_route = executed_source_reference is not None
    anchored_route = expected_code is not None or replacement_code is not None
    arguments = {
        "label": label,
        "block_labels": requested_labels,
        "parameters": runtime_parameters,
        "has_code_edit": True,
        "edit_source": "executed_source_reference" if reference_route else "anchored_replacement",
    }

    def fail_before_run(result: dict[str, Any]) -> str:
        _carry_unresolved_failure_into_result(copilot_ctx, result, "edit_block_and_run")
        record_tool_step_result_for_ctx(copilot_ctx, "edit_block_and_run", arguments, result)
        finalize_build_test_result(
            copilot_ctx,
            source_tool="edit_block_and_run",
            result=result,
            diagnosis_shadow_eligible=False,
        )
        return json.dumps(sanitize_tool_result_for_llm("edit_block_and_run", result))

    # Worded from what the call passed, so the reference is named only to a caller that sent one.
    edit_source_error = None
    if reference_route and anchored_route:
        edit_source_error = "Pass executed_source_reference without expected_code or replacement_code."
    elif not reference_route and (expected_code is None or replacement_code is None):
        edit_source_error = "Pass both expected_code and replacement_code."
    if edit_source_error:
        return fail_before_run({"ok": False, "error": edit_source_error, "error_code": "invalid_edit_source"})
    if label not in requested_labels:
        return fail_before_run(
            {
                "ok": False,
                "error": (
                    f"block_labels must include the edited block {label!r} so this call tests the persisted repair."
                ),
            }
        )
    authority_error = _authority_tool_error(copilot_ctx, "edit_block_and_run")
    if authority_error:
        return _diagnosis_repair_tool_error(copilot_ctx, "edit_block_and_run", authority_error)

    if executed_source_reference is not None:
        resolution = resolve_executed_browser_code_source(copilot_ctx, executed_source_reference)
        if resolution.status != "valid":
            return fail_before_run(
                {
                    "ok": False,
                    "error": (
                        f"The executed source reference is {resolution.status}. Run the complete candidate again "
                        "in this turn and browser session, then use its new reference."
                    ),
                    "error_code": "invalid_executed_source_reference",
                    "reference_status": resolution.status,
                }
            )

    handler_start = time.monotonic()
    skip_run_after_update = _update_and_run_requires_skipped_run(copilot_ctx, "edit_block_and_run")
    _clear_pending_browser_interaction_observation(copilot_ctx)

    try:
        if executed_source_reference is not None:
            async with browser_session_recovery(copilot_ctx):
                resolution = resolve_executed_browser_code_source(copilot_ctx, executed_source_reference)
                if resolution.status != "valid" or resolution.source is None:
                    return fail_before_run(
                        {
                            "ok": False,
                            "error": (
                                f"The executed source reference became {resolution.status} before persistence. "
                                "Run the complete candidate again and use its new reference."
                            ),
                            "error_code": "invalid_executed_source_reference",
                            "reference_status": resolution.status,
                        }
                    )
                stored_yaml = _stored_workflow_yaml(copilot_ctx)
                current_code = stored_block_code(stored_yaml, label, allow_empty=True)
                if current_code is None:
                    raise BlockEditError(f"Block {label!r} has no code to replace.")
                workflow_yaml = apply_block_edit(
                    stored_yaml,
                    label,
                    expected_code=current_code,
                    replacement_code=resolution.source,
                )
                prior_definition = await _get_prior_workflow_definition(copilot_ctx)
                workflow_yaml = preserve_untouched_block_configuration(
                    workflow_yaml, prior_definition, edited_label=label
                )
                with copilot_span("edit_block_and_run.update", data={"yaml_length": len(workflow_yaml)}):
                    update_result = await _update_workflow(
                        {
                            "workflow_yaml": workflow_yaml,
                            "_preserve_code_block_associations": True,
                            "_expected_exact_code_by_label": {label: resolution.source},
                            "_rebuilt_block_labels": [label],
                        },
                        copilot_ctx,
                        allow_missing_credentials=skip_run_after_update,
                        originating_call_id=_originating_call_id(ctx),
                        block_scoped_authoring_prior_yaml=preserve_untouched_block_configuration(
                            _stored_workflow_yaml(copilot_ctx), prior_definition, edited_label=label
                        ),
                    )
                    _record_workflow_update_result(copilot_ctx, update_result, prior_definition)
        else:
            workflow_yaml = apply_block_edit(
                _stored_workflow_yaml(copilot_ctx),
                label,
                expected_code=expected_code,
                replacement_code=replacement_code,
            )
            prior_definition = await _get_prior_workflow_definition(copilot_ctx)
            workflow_yaml = preserve_untouched_block_configuration(workflow_yaml, prior_definition, edited_label=label)
            with copilot_span("edit_block_and_run.update", data={"yaml_length": len(workflow_yaml)}):
                update_result = await _update_workflow(
                    {
                        "workflow_yaml": workflow_yaml,
                        "_preserve_code_block_associations": True,
                        "_rebuilt_block_labels": [label],
                    },
                    copilot_ctx,
                    allow_missing_credentials=skip_run_after_update,
                    originating_call_id=_originating_call_id(ctx),
                    block_scoped_authoring_prior_yaml=preserve_untouched_block_configuration(
                        _stored_workflow_yaml(copilot_ctx), prior_definition, edited_label=label
                    ),
                )
                _record_workflow_update_result(copilot_ctx, update_result, prior_definition)
    except BlockEditError as exc:
        return fail_before_run({"ok": False, "error": str(exc)})
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


@function_tool(failure_error_function=copilot_tool_failure, name_override="add_block", strict_mode=False)
async def add_block_tool(
    ctx: RunContextWrapper,
    after_label: str,
    block: dict[str, Any],
    parameters: list[dict[str, Any]] | None = None,
    code_artifact_metadata: list[CodeArtifactMetadata] | None = None,
    block_observation_refs: list[BlockObservationRef] | None = None,
) -> str:
    """Add one new block after an existing one, leaving every other block exactly as it is.

    Prefer this over resending the whole workflow whenever you are adding to a workflow that already
    exists: you send only the new block, so the blocks that already work cannot be disturbed and the workflow
    is not retyped. `after_label` must name a block that exists; the new block is linked in directly
    after it and inherits what that block pointed at.

    Pass `block` as a single block object including its `label`, with the same keys as a block in the
    workflow YAML; multiline code is a plain JSON string. Declare any new top-level
    workflow parameters the block reads in `parameters` — a new block and the parameter it consumes
    have to land in the same call, or the workflow is briefly saved in a state that cannot run. For a
    code block pass its `code_artifact_metadata` row here too, since a brand-new block has none yet.

    Each workflow parameter is a flat object, for example
    `{"parameter_type": "workflow", "key": "...", "workflow_parameter_type": "string", "default_value": "..."}`.
    Take its `default_value` from the page when inspecting or running the saved workflow shows one.

    To change a block that already exists use edit_block; to remove one use delete_block.
    """
    block_yaml = dump_workflow_yaml(block)
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


@function_tool(failure_error_function=copilot_tool_failure, name_override="delete_block")
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


@function_tool(failure_error_function=copilot_tool_failure, name_override="list_credentials")
async def list_credentials_tool(
    ctx: RunContextWrapper,
    page: int = 1,
    page_size: int = 10,
    exact_reference: str | None = None,
) -> str:
    """List stored credentials (metadata only — never passwords or secrets).
    Use this to find the credential IDs a workflow can log in with.

    When the agent selects a saved name or credential ID that appears as a complete
    credential reference in the latest literal user turn, or is already selected in the saved
    workflow or a credential-card reply, pass it as `exact_reference` without asking again.
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


@function_tool(failure_error_function=copilot_tool_failure, name_override="request_credential")
async def request_credential_tool(
    ctx: RunContextWrapper,
    login_page_url: str,
    reason: str,
    credential_id: str | None = None,
    rejected_by_site: bool = False,
    registration: CredentialRegistration | None = None,
) -> str:
    """Ask the user, in chat, how to sign in to a sign-in page: add or pick a saved credential, or
    sign in themselves in the live browser and save that sign-in as a browser profile.

    Use this instead of a prose question when a login still needs selection or setup, including
    a sign-in page discovered while navigating, or a saved browser profile whose sign-in no longer
    reaches past the sign-in page. Reuse the user's existing choice, the saved
    workflow's binding, or an unambiguous website match without asking again when it can fill.
    `login_page_url` is the absolute HTTP(S) sign-in URL shown on the card; selecting a credential
    authorizes it for that site. `reason` is one sentence explaining why the login is needed.
    Pass `credential_id` when that selected credential reached a verification-code step it has no
    authenticator for, so the card asks the user to add one to it instead of picking a login.
    Pass `credential_id` with `rejected_by_site=true` when the run and page evidence show the site
    refused that bound credential's saved password or code; a failed run's `credential_update` lever
    names the one credential bound to its failed block. The card asks the user to update it and
    comes back `updated` once they save; re-run the sign-in then.
    When the user asked you to create one new account and the sign-up form needs a password, pass
    `registration` with the sign-up page as `login_page_url`: the card offers to generate a password
    and save it with that username and credential name, and you only ever get the credential's id.
    `password_length` is 24 to 128; a longer or shorter site rule comes back `unsupported_constraints`.
    `charset` is `alphanumeric` or `alphanumeric_symbols`; when the site's rules fit neither, call
    without `registration` so the user adds the login. Never write a password yourself.
    A second `registration` ask on a turn that already generated a credential, or may have, comes
    back `already_generated` with no card.
    The call waits for the user's answer and comes back `connected` with the credential
    to bind, `skipped`, `unanswered`, or `unavailable` — follow the `next` or `fallback` it
    carries. It comes back `signed_in` when the user signed in themselves in the live browser:
    no credential was provided, and the result names the browser profile that saved the sign-in.
    Each turn allows one ask to pick a login and one ask to update a chosen credential.
    """
    copilot_ctx = ctx.context
    arguments = {
        "login_page_url": login_page_url,
        "reason": reason,
        "credential_id": credential_id,
        "rejected_by_site": rejected_by_site,
        "registration": registration.model_dump() if registration else None,
    }
    result: dict[str, Any] = {}
    try:
        authority_error = _authority_tool_error(copilot_ctx, "request_credential")
        if authority_error:
            result = {"ok": False, "error": authority_error}
        else:
            result = await _request_credential(
                login_page_url,
                reason,
                copilot_ctx,
                credential_id,
                rejected_by_site,
                anchor_tool_call_id=_originating_call_id(ctx),
                registration=registration,
            )
    finally:
        # A card on screen right now owns the gate; this call must not open it for one it never
        # raised. Every other exit has to release, including a repeat ask in a later response.
        if not copilot_ctx.credential_ask_in_flight:
            release_credential_pause_gate(copilot_ctx)
    record_tool_step_result_for_ctx(copilot_ctx, "request_credential", arguments, result)
    return json.dumps(result)


@function_tool(failure_error_function=copilot_tool_failure, name_override="ask_user")
async def ask_user_tool(ctx: ToolContext[CopilotContext], parts: list[QuestionInput]) -> str:
    """Ask the user questions in an inline card when information is required before you can continue, and
    receive their response in this same call.

    Keep questions and choice labels concise. Aim for 200 characters or fewer per question or choice, and offer no more than eight choices.

    Use it for general preferences, missing information, partial answers, or clarification. Give one
    part per thing you need, each with a prompt and optional choices. The card always has a bottom
    free-text field. Users can submit choices, free text, or both, answer some parts, or skip. The result
    preserves the questions, chosen options, free text, and unanswered parts. Decide what to do
    next from those facts, including explaining or asking again. A choice the user picked is their
    decision: act on that option, never on a different option or a default of your own; if it still
    needs a detail they did not give, ask for that detail. When something only the user can supply is
    still missing, including a part they skipped or left unanswered, ask for it again with this tool,
    offering choices when there are only a few possible answers, rather than listing it in your reply.
    Use request_credential for credentials; an ordinary answer does not change existing action or
    credential permissions.

    When you already prefer one choice, set `recommended` on it; the card lists it first and tags
    it. The card always ends the choices with its own "Other" that takes a typed answer, so never
    add a catch-all choice such as "Other" or "Something else". Give a choice a `detail_prompt`
    when it cannot be acted on without more detail ("Choose another restaurant" with "Which
    restaurant?"); the user must type that detail to pick it, and a part whose status is
    `detail_missing` came back without it.
    """
    from skyvern.forge.sdk.copilot.ask_user import ask_user

    return json.dumps(await ask_user(ctx.context, AskUserArguments(parts=parts), ctx.tool_call_id))


@function_tool(
    failure_error_function=copilot_tool_failure,
    name_override=ACCOUNT_GROUP_SUBMIT_TOOL_NAME,
    description_override=ACCOUNT_GROUP_SUBMIT_TOOL_DESCRIPTION,
    strict_mode=False,
)
async def run_workflow_for_accounts_tool(
    ctx: ToolContext[CopilotContext],
    credential_ids: list[str],
    credential_parameter_key: str,
    action_summary: str,
    common_inputs: dict[str, JsonValue] | None = None,
) -> str:
    authority_error = _authority_tool_error(ctx.context, ACCOUNT_GROUP_SUBMIT_TOOL_NAME)
    if authority_error is not None:
        return json.dumps({"ok": False, "dispatched": False, "error": authority_error})
    result = await run_for_accounts(
        ctx.context,
        tool_call_id=ctx.tool_call_id,
        credential_ids=credential_ids,
        credential_parameter_key=credential_parameter_key,
        common_inputs=common_inputs or {},
        action_summary=action_summary,
    )
    return result.model_dump_json(exclude_none=True, exclude={"repeated_after_prior_effect": {"__all__": {"identity"}}})


@function_tool(failure_error_function=copilot_tool_failure, name_override=ACCOUNT_GROUP_STATUS_TOOL_NAME)
async def get_account_group_status_tool(ctx: ToolContext[CopilotContext], workflow_run_group_id: str) -> str:
    """Read each account's run id, status and outcome for a group started by run_workflow_for_accounts.
    A failed outcome means the run stopped before it could have acted; unknown means it may have acted."""
    return (await account_group_status(ctx.context, workflow_run_group_id)).model_dump_json(exclude_none=True)


@function_tool(failure_error_function=copilot_tool_failure, name_override=ACCOUNT_GROUP_CANCEL_TOOL_NAME)
async def cancel_account_group_tool(ctx: ToolContext[CopilotContext], workflow_run_group_id: str) -> str:
    """Ask the user to stop every unfinished run of a group this chat started with run_workflow_for_accounts.
    Nothing is canceled unless the user approves; returns every row's status and whether they approved."""
    result = await cancel_account_group(
        ctx.context, tool_call_id=ctx.tool_call_id, workflow_run_group_id=workflow_run_group_id
    )
    return result.model_dump_json(exclude_none=True)


@function_tool(
    failure_error_function=copilot_tool_failure,
    name_override=CREDENTIAL_DELETE_TOOL_NAME,
    description_override=CREDENTIAL_DELETE_TOOL_DESCRIPTION,
)
async def delete_saved_credentials_tool(ctx: ToolContext[CopilotContext], credential_ids: list[str]) -> str:
    return json.dumps(
        await delete_saved_credentials(ctx.context, tool_call_id=ctx.tool_call_id, credential_ids=credential_ids)
    )


# This description is measured, not prose: a storage-fidelity sentence in it took authoring from 15/20 to 3/20.
# Re-measure with the arms in cloud_docs/workflow-copilot/architecture/offline-replay.md before editing.
@function_tool(failure_error_function=copilot_tool_failure, name_override="set_work_plan")
async def set_work_plan_tool(ctx: ToolContext[CopilotContext], items: list[str]) -> str:
    """Replace your own work plan for this chat with `items`, in the order you mean to do them.

    This is your working memory, not a report to the user and not evidence of anything: nothing
    you write here satisfies a requirement, proves an output, or changes what gets tested or run.
    It survives across turns and comes back to you at the start of every later model call, so use
    it to hold onto responsibilities you have not reached yet — the steps past the one you are
    working on right now.

    Every call replaces the whole list; there is no append and no per-item update, so send the
    full plan as you now believe it, and send an empty list to clear it. Rewrite it whenever what
    you learned changes what is left: after scouting, after authoring, after a test, after a
    repair.
    """
    return json.dumps(await set_work_plan(ctx.context, WorkPlanArguments(items=items)))


@function_tool(failure_error_function=copilot_tool_failure, name_override="list_integrations")
async def list_integrations_tool(ctx: RunContextWrapper) -> str:
    """List the organization's connected Google and Microsoft accounts (metadata only —
    never tokens). Each entry has `connection_id`, `provider`, `name`, `state`,
    `email_address`, and `scopes_granted`.

    These are OAuth connections made on the Integrations page, NOT the stored
    login credentials returned by `list_credentials` — the two lists are disjoint,
    so check this one before concluding the user has no Google or Microsoft access.
    Prefer a purpose-built native integration block over automating that connected
    service through its browser UI. For Google Sheets, author a `google_sheets_read`
    or `google_sheets_write` block and pass an active compatible connection's
    `connection_id` as its `credential_id`. Not paginated; one call returns every
    connection.

    Match on `scopes_granted`, not on `provider` alone: connections are granted per
    product, so a Sheets connection cannot read Gmail and binding it to a mail block
    fails at run time. A connection whose `state` is `active` can mint an access token.
    A connection whose `state` is `error` remains listed but cannot mint one until it
    is authorized again.

    When the current request identifies one active, scope-compatible row, bind its
    exact `connection_id` as the native block's `credential_id` and continue.
    Never require an opaque ID already present in this tool result to be repeated.
    If the requested account remains ambiguous or no compatible active row exists,
    use a grounded ordinary-language clarification instead.

    A `credential_id` may instead be the connection `name` or `email_address`
    exactly as the user wrote it in this turn. The server resolves it and reports
    the outcome under `google_connection_resolution` in the result of the call
    that saves the workflow: `resolved` rewrites the slot to the `connection_id`, while `ambiguous`,
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


@function_tool(failure_error_function=copilot_tool_failure, name_override="read_google_sheet")
async def read_google_sheet_tool(
    ctx: RunContextWrapper,
    spreadsheet_url: str,
    connection_id: str | None = None,
    range: str | None = None,
) -> str:
    """Read a Google spreadsheet through the organization's connected Google accounts, without a
    browser or a workflow run. Read-only; returns values, never tokens or formulas.

    `spreadsheet_url` must be a Google Sheets URL the user wrote in this chat; any other
    spreadsheet is not read. With `connection_id` omitted, every active Sheets-scoped connection
    is tried. `connections` reports one row per connection with `connection_id`, `name`,
    `email_address` and a `status`: `opened` (the connection reads this spreadsheet), `no_access`
    (the account cannot see this spreadsheet), `token_unavailable` (no access token could be
    minted for the connection), `error` (the attempt failed; see `reason`), or `not_eligible`
    (the given `connection_id` is not an active Sheets-scoped connection).

    When a connection opened the spreadsheet the result also has `title`, `tabs` (each with
    `title`, `gid`, `row_count`, `column_count`), `read_through` (the connection the values came
    from) and `values` for `range`. `range` is A1 notation; with it omitted, `values` holds the
    first rows of the tab named by the URL's `gid` (the first tab when the URL has none).
    `truncated` is true when rows, columns or cell text were cut to fit.
    """
    copilot_ctx = ctx.context
    arguments: dict[str, Any] = {"spreadsheet_url": spreadsheet_url, "connection_id": connection_id, "range": range}
    authority_error = _authority_tool_error(copilot_ctx, "read_google_sheet")
    if authority_error:
        result = {"ok": False, "error": authority_error}
        record_tool_step_result_for_ctx(copilot_ctx, "read_google_sheet", arguments, result)
        return json.dumps(result)

    result = await _read_google_sheet(arguments, copilot_ctx)
    record_tool_step_result_for_ctx(copilot_ctx, "read_google_sheet", arguments, result)
    sanitized = sanitize_tool_result_for_llm("read_google_sheet", result)
    return json.dumps(sanitized)


@function_tool(failure_error_function=copilot_tool_failure, name_override="get_organization_usage_quota")
async def get_organization_usage_quota_tool(ctx: RunContextWrapper) -> str:
    """Read this organization's account usage when the user asks about usage, credits, quota, plan
    or billing state: plan tier, current billing period, included credits, credits consumed,
    credits remaining, top-up credits and overage status.

    A null field means the account does not record it - report it as unavailable and point at
    `billing_page_path`, never as zero.
    """
    copilot_ctx = ctx.context
    arguments: dict[str, Any] = {}
    authority_error = _authority_tool_error(copilot_ctx, "get_organization_usage_quota")
    if authority_error:
        result: dict[str, Any] = {"ok": False, "error": authority_error}
        record_tool_step_result_for_ctx(copilot_ctx, "get_organization_usage_quota", arguments, result)
        return json.dumps(result)

    usage = await app.AGENT_FUNCTION.get_organization_usage_quota(
        organization_id=copilot_ctx.organization_id,
    )
    result = {"ok": True, "data": asdict(usage)}
    record_tool_step_result_for_ctx(copilot_ctx, "get_organization_usage_quota", arguments, result)
    sanitized = sanitize_tool_result_for_llm("get_organization_usage_quota", result)
    return json.dumps(sanitized)


@function_tool(
    failure_error_function=copilot_tool_failure,
    name_override="run_blocks_and_collect_debug",
    timeout=RUN_BLOCKS_SAFETY_CEILING_SECONDS,
    strict_mode=False,
)
async def run_blocks_tool(
    ctx: RunContextWrapper,
    block_labels: list[str],
    parameters: dict[str, Any] | None = None,
    new_exit: bool = False,
) -> Any:
    """Run one or more blocks of the current workflow, wait for completion,
    and return compact debug output (status, failure reason, visible elements).
    The workflow must be saved before running blocks.
    Block labels must match labels in the saved workflow.
    A run that starts at the first block opens a fresh browser loaded with the saved browser
    profile, if any, that a normal run of the saved workflow would load. A profile that is only
    staged in a proposal is not loaded until the workflow is saved with it, except the profile the
    user saved by signing in from the credential card this turn: a draft that selects it loads it now.

    If an existing saved block can establish the state you need, run that
    block unchanged before scouting the resulting page.

    Pass runtime values for workflow parameters via the `parameters` dict —
    keys must match the workflow parameter `key` field. For a raw secret value (for example, a password), call
    `list_credentials` and use a credential parameter whose default_value is
    the stored `credential_id`. If no stored credential matches, do NOT pass
    the inline value via `parameters`. Ask the user to store it as a saved
    credential and reply with the credential name; do not build or run with
    the raw value.

    Use browser inspection and run evidence to fill knowledge gaps before
    changing the workflow. If visible state is uncertain, inspect the live
    page and then compose the next normal workflow action from observed
    evidence instead of retrying guessed URL params or page structure.

    `new_exit` runs in a new browser whose network exit is verified to differ
    from the one the previous build test in this chat used; the result's
    `new_exit` receipt says whether that happened or why no different exit
    was available, in which case nothing runs.
    """
    copilot_ctx = ctx.context
    await await_pending_credential_pause(copilot_ctx)
    copilot_ctx.completion_verification_result = None
    handler_start = time.monotonic()
    arguments = {"block_labels": block_labels, "parameters": parameters or {}, "new_exit": new_exit}
    authority_error = _authority_tool_error(copilot_ctx, "run_blocks_and_collect_debug")
    if authority_error:
        return _diagnosis_repair_tool_error(copilot_ctx, "run_blocks_and_collect_debug", authority_error)

    prior_definition = await _get_prior_workflow_definition(copilot_ctx)
    labels_to_execute, block_outputs_to_seed, frontier_start_label, start_provenance = _plan_frontier(
        copilot_ctx,
        block_labels,
        prior_definition,
        prior_definition,
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
        with pending_operation("tool.run_blocks_and_collect_debug"):
            result = await _run_blocks_and_collect_debug(
                arguments,
                copilot_ctx,
                labels_to_execute=labels_to_execute,
                block_outputs_to_seed=block_outputs_to_seed,
                frontier_start_label=frontier_start_label,
                new_exit=new_exit,
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


@function_tool(
    failure_error_function=copilot_tool_failure,
    name_override="test_workflow_from_blank_browser",
    timeout=RUN_BLOCKS_SAFETY_CEILING_SECONDS,
    strict_mode=False,
)
async def test_workflow_from_blank_browser_tool(
    ctx: RunContextWrapper, parameters: dict[str, Any] | None = None
) -> str:
    """Test every block of the staged candidate, or current canonical workflow, in order.

    Uses a separate blank browser with no scouting state and no restored saved browser profile.
    This tests code-established prerequisites, not configured authenticated-profile behavior;
    the workflow's saved profile settings are retained. Choose this test when useful; ordinary
    partial-run tools keep their current scope. Supply runtime workflow inputs in parameters.
    For a raw secret value (for example, a password), use a saved credential reference instead.
    """
    copilot_ctx = ctx.context
    await await_pending_credential_pause(copilot_ctx)
    result = await run_workflow_end_to_end(copilot_ctx, parameters=parameters)
    record_tool_step_result_for_ctx(
        copilot_ctx, "test_workflow_from_blank_browser", {"parameters": parameters or {}}, result
    )
    enqueue_screenshot_from_result(
        copilot_ctx,
        result,
        provenance=_run_result_screenshot_provenance(result, source_tool="test_workflow_from_blank_browser"),
    )
    return json.dumps(sanitize_tool_result_for_llm("test_workflow_from_blank_browser", result))


@function_tool(failure_error_function=copilot_tool_failure, name_override="get_run_results")
async def get_run_results_tool(
    ctx: RunContextWrapper,
    workflow_run_id: str | None = None,
    block_cursor: str | None = None,
    row_keys: list[str] | None = None,
) -> str:
    """Fetch results from a previous workflow run.
    Returns block statuses, failure reasons, and output data.
    blocks is an index with one row per block execution, oldest first, up to 20
    rows per page. Each row has a row_key (its workflow_run_block_id, or registered:<label>
    for an output with no block row), its loop position
    (parent_workflow_run_block_id, current_index, current_value_preview) and the size
    and a short preview of its output and extracted_data. total_block_rows counts every
    row; next_block_cursor is present while rows remain, and passing it as block_cursor
    returns the next page. Run-level fields come with the first page only.
    row_keys (at most 25) returns block_details instead of the index: each named row's
    complete output and extracted_data plus its action observations. Rows that do not
    fit one call are listed in deferred_row_keys; a row too large for any call returns
    its size, a preview and child_count, and its iterations are readable as their own rows.
    If workflow_run_id is omitted, fetches the run this chat carries: its last
    successful test run, else the last run it tested or was opened about. When
    it carries none, fetches the most recently created finished run
    (completed, failed, canceled, terminated, or timed_out) not started by a
    Copilot chat. selected_by says which of these happened ("carried_from_chat",
    "latest_for_workflow", or "explicit" when workflow_run_id was passed), and
    created_at and trigger_type describe the returned run.
    newer_finished_runs lists finished runs of this workflow created after the
    returned run, excluding runs started by any Copilot chat, newest first; it
    holds at most 5, so more may exist. When that lookup fails the result has
    newer_finished_runs_unavailable instead, so a missing list is not "none".
    execution_source with source_kind run_version describes the saved version the run executed, which may
    differ from the current draft (current_draft_code_matches compares each code block); disposition
    unavailable means that version cannot be proven, so no executed source is given.
    """
    copilot_ctx = ctx.context
    params: dict[str, Any] = {}
    if workflow_run_id:
        params["workflow_run_id"] = workflow_run_id
    authority_error = _authority_tool_error(copilot_ctx, "get_run_results")
    if authority_error:
        return json.dumps({"ok": False, "error": authority_error})
    offset = 0
    if block_cursor is not None:
        parsed_cursor = parse_run_results_cursor(block_cursor)
        if parsed_cursor is None:
            return json.dumps(
                {"ok": False, "error": f"block_cursor {block_cursor!r} is not a cursor this tool returned."}
            )
        cursor_run_id, offset = parsed_cursor
        if workflow_run_id and workflow_run_id != cursor_run_id:
            return json.dumps(
                {"ok": False, "error": f"block_cursor pages {cursor_run_id}, not workflow_run_id {workflow_run_id}."}
            )
        params["workflow_run_id"] = cursor_run_id
    if row_keys is not None:
        row_keys = list(dict.fromkeys(row_keys))
        if len(row_keys) > RUN_RESULTS_MAX_ROW_KEYS:
            return json.dumps(
                {"ok": False, "error": f"row_keys holds {len(row_keys)} keys; pass at most {RUN_RESULTS_MAX_ROW_KEYS}."}
            )
    first_page = block_cursor is None and row_keys is None
    result = await _get_run_results(params, copilot_ctx, read_live_page=first_page, skip_page_evidence=not first_page)
    record_tool_step_result_for_ctx(copilot_ctx, "get_run_results", params, result)
    if result.get("ok") is not False:
        # Exact-match scrubbing has to see whole strings, before any preview cuts or re-serializes them.
        result = scrub_secrets_from_structure(copilot_ctx, result)
        run_rows = await _chronological_run_block_rows(result["data"]["workflow_run_id"], copilot_ctx.organization_id)
        loop_facts = scrub_secrets_from_structure(copilot_ctx, run_block_loop_facts(run_rows))
        result = project_run_results_page(result, loop_facts, offset=offset, row_keys=row_keys)

    sanitized = sanitize_tool_result_for_llm("get_run_results", result)
    return json.dumps(scrub_secrets_from_structure(copilot_ctx, sanitized))


def _promote_executed_sources(
    copilot_ctx: CopilotContext, update_params: dict[str, Any], references: dict[str, str] | None
) -> dict[str, Any] | None:
    """Write each referenced cell's exact source into its block of the submitted YAML, or return the refusal."""
    if not references:
        return None
    promoted: dict[str, str] = {}
    workflow_yaml = update_params["workflow_yaml"]
    for label, reference in references.items():
        resolution = resolve_executed_browser_code_source(copilot_ctx, reference)
        if resolution.status != "valid" or resolution.source is None:
            return {
                "ok": False,
                "error": (
                    f"The executed source reference for block {label!r} is {resolution.status}. Run the complete "
                    "candidate again in this turn and browser session, then use its new reference."
                ),
                "error_code": "invalid_executed_source_reference",
                "reference_status": resolution.status,
            }
        current_code = stored_block_code(workflow_yaml, label, allow_empty=True)
        try:
            if current_code is None:
                raise BlockEditError(f"Block {label!r} is not a code block in `workflow`; give it an empty `code`.")
            workflow_yaml = apply_block_edit(
                workflow_yaml, label, expected_code=current_code, replacement_code=resolution.source
            )
        except BlockEditError as exc:
            return {"ok": False, "error": str(exc)}
        promoted[label] = resolution.source
    update_params["workflow_yaml"] = workflow_yaml
    update_params["_expected_exact_code_by_label"] = promoted
    update_params["_rebuilt_block_labels"] = sorted(promoted)
    return None


@function_tool(
    failure_error_function=copilot_tool_failure,
    name_override=UPDATE_AND_RUN_BLOCKS_TOOL_NAME,
    timeout=RUN_BLOCKS_SAFETY_CEILING_SECONDS,
    strict_mode=False,
    tool_input_guardrails=[_WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL],
)
async def update_and_run_blocks_tool(
    ctx: RunContextWrapper,
    workflow: dict[str, Any],
    block_labels: list[str],
    block_observation_refs: list[BlockObservationRef] | None = None,
    code_artifact_metadata: list[CodeArtifactMetadata] | None = None,
    parameters: dict[str, Any] | None = None,
    executed_source_references: dict[str, str] | None = None,
) -> Any:
    """Update the workflow and immediately run the specified blocks in one step.
    Pass the complete workflow as a `workflow` object with the same keys as the workflow YAML, as in
    update_workflow; string values, including multiline code, are plain JSON strings.
    This persists the workflow and remotely executes the selected frontier, waiting for it to
    finish, so it is materially higher latency than a bounded page read. It is the surface for
    testing durable behaviour, and for reaching a state that only execution can establish --
    authentication, a credential or OTP step, or state an upstream block creates.
    Use this instead of calling update_workflow and run_blocks_and_collect_debug separately.
    The workflow must validate successfully before blocks are run.

    The result carries `data.stored_code` — the code each changed block now holds, which is what a
    following edit must anchor to — plus `data.stored_code_rewritten` for the labels the server
    rewrote away from what you submitted and `data.stored_code_withheld` for any too large to return.

    `block_labels` may be a tested frontier subset of the full workflow;
    save the complete reusable workflow, then run only the next 1-2 unverified
    blocks when a long form/search/result chain can be verified incrementally.

    Top-level workflow parameter keys appear in the run-input UI. When you
    add runtime inputs in `workflow_definition.parameters`, name keys for the
    reusable domain value the user supplies, not the page widget or action used
    to enter it.

    Pass runtime values for workflow parameters via the `parameters` dict —
    keys must match the workflow parameter `key` field. For a raw secret value (for example, a password), call
    `list_credentials` and use a credential parameter whose default_value is
    the stored `credential_id`. If no stored credential matches, do NOT pass
    the inline value via `parameters`. Ask the user to store it as a saved
    credential and reply with the credential name; do not build or run with
    the raw value.

    Use browser inspection and run evidence to fill knowledge gaps while
    building, editing, or debugging the workflow. Do not invent URL params,
    form fields, result affordances, or page structure from memory; ground
    workflow blocks in observed MCP evidence or information the user supplied.
    Browser inspection is build-time context; add durable workflow blocks only
    for the reusable actions/checks the workflow actually needs.
    When you compose no-url blocks from a page reached by prior clicks, include
    `block_observation_refs` entries with each block label and the
    `observation_step` returned by inspect_page_for_composition (or another
    page read) for the page that block acts on.
    For authored code blocks, include `code_artifact_metadata` rows describing
    declared goals, claimed outcomes, page dependencies, criteria, evidence
    refs, observation refs, and terminal verifier expectations.
    """
    workflow_yaml = dump_workflow_yaml(workflow)
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
        "executed_source_labels": sorted(executed_source_references or {}),
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
    update_params: dict[str, Any] = {
        "workflow_yaml": workflow_yaml,
        "block_observation_refs": normalized_block_observation_refs,
        "code_artifact_metadata": serialized_code_artifact_metadata,
        "raw_code_artifact_metadata": code_artifact_metadata,
        "block_labels": block_labels,
        "parameters": parameters or {},
    }
    # Held across the write like edit_block_and_run's promotion: a reference is only valid for the
    # browser continuity it was minted in, and recovery must not change that between resolve and save.
    async with browser_session_recovery(copilot_ctx) if executed_source_references else nullcontext():
        promotion_error = _promote_executed_sources(copilot_ctx, update_params, executed_source_references)
        if promotion_error is None:
            with copilot_span("update_workflow", data={"yaml_length": len(update_params["workflow_yaml"])}):
                update_result = await _update_workflow(
                    update_params,
                    copilot_ctx,
                    allow_missing_credentials=skip_run_after_update,
                    originating_call_id=_originating_call_id(ctx),
                )
                _record_workflow_update_result(copilot_ctx, update_result, prior_definition)
        else:
            update_result = promotion_error

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
    policy = copilot_ctx.request_policy
    skip_result = {
        "ok": True,
        "message": (
            _credential_deferred_draft_message(copilot_ctx)
            if policy is not None and policy.raw_secret_redacted_draft
            else "Skipped test run: required credentials are not configured."
        ),
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
    prior_definition: WorkflowDefinition | None,
    block_labels: list[str],
    parameters: dict[str, Any],
    handler_start: float,
) -> str:
    """Run a just-persisted definition through the shared frontier and debug-evidence seam."""
    new_definition = copilot_ctx.last_workflow.workflow_definition if copilot_ctx.last_workflow is not None else None

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


@function_tool(
    failure_error_function=copilot_tool_failure, name_override="discover_workflow_entrypoint", strict_mode=False
)
async def discover_workflow_entrypoint_tool(
    ctx: RunContextWrapper,
    site_or_url: str,
    intent_hint: str,
) -> str:
    """Find the page a new workflow should start at on a site whose URL or domain you have.

    Use this BEFORE writing blocks when you know the site but not the specific
    page. Accepts a URL with or without scheme (``example.com/login`` is fine)
    or a bare domain (``example.com``). A site name returns
    ``failure_reason=could_not_resolve_site_name``: pass a URL you already have
    for it, such as one the user gave or a credential's ``tested_url``, or find
    it with ``search_web``.

    Returns ``candidate_url`` plus a short ``evidence_trail`` and any
    ``candidate_form_fields``. Use ``candidate_url`` as the ``url`` value on a
    ``goto_url`` block. Do NOT paste the evidence into workflow YAML.

    Discovery navigates and reads pages; it will NOT type, click form buttons,
    run JavaScript, or submit forms.
    """
    authority_error = _authority_tool_error(ctx.context, "discover_workflow_entrypoint")
    if authority_error:
        return _diagnosis_repair_tool_error(ctx.context, "discover_workflow_entrypoint", authority_error)
    result = await _discover_workflow_entrypoint_impl(ctx.context, site_or_url, intent_hint)
    return json.dumps(scrub_secrets_from_structure(ctx.context, result))


@function_tool(failure_error_function=copilot_tool_failure, name_override="search_web", strict_mode=False)
async def search_web_tool(ctx: RunContextWrapper, query: str, max_results: int = 10) -> str:
    """Search the web for pages matching a query, when you need candidate sites rather than one known page.

    It also finds the URL of a site you know only by name. ``results`` holds
    up to ``max_results`` (1 to 100) entries with ``title``, ``url`` and ``snippet``, each
    ``url`` a direct absolute link to the result site.

    The rest of the reply is what the search actually did, so you can tell the
    cases apart yourself: ``extracted_count`` is how many results the search
    returned, ``withheld_count`` how many of those were withheld because they fall
    outside the query's ``site:`` filter or their destination is not allowed, ``http_status`` the status of the
    search API request whose results you got, and ``error_kind`` the failure if the search failed without
    returning anything. An empty ``results`` with a non-zero ``withheld_count``
    is a filtered search; with ``extracted_count`` zero and no ``error_kind`` the
    query found nothing. Do not report a failed search as "no matches".

    This does not touch the scouting tab. The same search is available inside a
    code block as ``await search_web(query, max_results=10)``, returning the same shape.
    """
    authority_error = _authority_tool_error(ctx.context, "search_web")
    if authority_error:
        return _diagnosis_repair_tool_error(ctx.context, "search_web", authority_error)
    result = await _search_web_impl(query, max_results)
    return json.dumps(scrub_secrets_from_structure(ctx.context, result))


@function_tool(failure_error_function=copilot_tool_failure, name_override=SOLVE_TOOL_NAME)
async def solve_page_challenge_tool(ctx: RunContextWrapper, image: str | None = None, input: str | None = None) -> str:
    """Run the platform captcha solver on the current page of this chat's browser.

    Use it when the page shows a human-verification or anti-bot challenge: a browser result's
    `page_state.challenge_vendor`, or a challenge you see in a screenshot. With no arguments it detects
    reCAPTCHA, hCaptcha and Cloudflare Turnstile widgets, including ones inside frames, and DataDome and
    PerimeterX challenge pages, and can take up to 120 seconds.

    For a distorted-text image CAPTCHA, pass `image`, the selector of its <img>, <svg> or <canvas> (or a
    container holding exactly one), and `input`, the selector of its answer field. The OCR a saved code
    block's `solve_captcha(page, image=..., input=...)` uses reads that image and types the text into that
    field, so selectors that work here are the ones to save. It requires image OCR enabled for the
    organization.

    `outcome` is one of: `solved`; `typed` (image form: the text was typed, unconfirmed until the page
    accepts it); `none` (no challenge detected, nothing ran); `unsupported` (a challenge frame is on screen
    that the solver has no route for); `unsolved` (with `timed_out`, `solver_failed` or, for the
    image form, `read_limit_reached` when that is why); or `unavailable` (solving or image OCR is off for
    this organization or page). `solved` and `typed` are the solver's report, not proof the page moved on:
    look at the page again before continuing. Each attempt can bill an external solver.

    For a widget, `unsolved` and `unsupported` describe this browser session only. Many sites decide per
    browser whether to challenge, from its cookies and history, so a new session from `start_fresh_browser`
    is a separate attempt; the result says whether this request has made it yet.
    """
    authority_error = _authority_tool_error(ctx.context, SOLVE_TOOL_NAME)
    if authority_error:
        return _diagnosis_repair_tool_error(ctx.context, SOLVE_TOOL_NAME, authority_error)
    result = await solve_page_challenge(ctx.context, image=image, input=input)
    arguments = {} if image is None and input is None else {"image": image, "input": input}
    record_tool_step_result_for_ctx(ctx.context, SOLVE_TOOL_NAME, arguments, result)
    return json.dumps(scrub_secrets_from_structure(ctx.context, result))


@function_tool(failure_error_function=copilot_tool_failure, name_override=FRESH_BROWSER_TOOL_NAME)
async def start_fresh_browser_tool(ctx: RunContextWrapper) -> str:
    """Replace this chat's browser with a new browser session, and continue in the new one.

    The new session has no cookies, storage, sign-ins or challenge history, so a site that challenged
    or blocked the old browser may not challenge it. Everything in the old browser is lost, including
    open tabs and the current page. `old_browser_closed` says whether the
    old browser was shut down; when the studio browser pane streams it, it is kept (`pane_kept_old`) and
    the pane keeps showing it while your browser tools act in the new one. If the new browser cannot
    start, the old one stays in use.
    """
    authority_error = _authority_tool_error(ctx.context, FRESH_BROWSER_TOOL_NAME)
    if authority_error:
        return _diagnosis_repair_tool_error(ctx.context, FRESH_BROWSER_TOOL_NAME, authority_error)
    result = await start_fresh_browser(ctx.context)
    record_tool_step_result_for_ctx(ctx.context, FRESH_BROWSER_TOOL_NAME, {}, result)
    return json.dumps(scrub_secrets_from_structure(ctx.context, result))


@function_tool(
    failure_error_function=copilot_tool_failure,
    name_override=SESSION_EXTENSION_TOOL_NAME,
    description_override=SESSION_EXTENSION_TOOL_DESCRIPTION,
)
async def extend_browser_session_tool(ctx: RunContextWrapper, additional_minutes: int) -> str:
    copilot_ctx = ctx.context
    arguments = {"additional_minutes": additional_minutes}
    authority_error = _authority_tool_error(copilot_ctx, SESSION_EXTENSION_TOOL_NAME)
    if authority_error:
        result: dict[str, Any] = {"ok": False, "error": authority_error}
    else:
        result = await extend_browser_session(copilot_ctx, additional_minutes)
    record_tool_step_result_for_ctx(copilot_ctx, SESSION_EXTENSION_TOOL_NAME, arguments, result)
    return json.dumps(scrub_secrets_from_structure(copilot_ctx, result))


@function_tool(failure_error_function=copilot_tool_failure, name_override=UPLOAD_TOOL_NAME)
async def upload_attached_file_tool(ctx: RunContextWrapper, file_id: str, selector: str) -> str:
    """Set a file the user attached to this chat (`file_id` exactly as listed in the attached files) on the page's
    <input type="file"> named by `selector`, without clicking any submit control; `ok` is true only when the input's
    own file list (`input_files`) then holds this file's name and size, and an unlisted or removed attachment is
    refused."""
    authority_error = _authority_tool_error(ctx.context, UPLOAD_TOOL_NAME)
    if authority_error:
        return _diagnosis_repair_tool_error(ctx.context, UPLOAD_TOOL_NAME, authority_error)
    result = await upload_attached_file(ctx.context, file_id, selector)
    record_tool_step_result_for_ctx(ctx.context, UPLOAD_TOOL_NAME, {"file_id": file_id, "selector": selector}, result)
    return json.dumps(scrub_secrets_from_structure(ctx.context, result))


@function_tool(
    failure_error_function=copilot_tool_failure, name_override="inspect_page_for_composition", strict_mode=False
)
async def inspect_page_for_composition_tool(
    ctx: RunContextWrapper,
    target_url: str,
    requested_output_reads: list[RequestedOutputRead] | None = None,
    target: BrowserTarget = BrowserTarget.DEBUG,
) -> str:
    """Inspect a known page before composing form/search workflow blocks.

    This is a bounded read of known or current page state: it is the surface for uncertainty about
    controls, selectors, visible state or layout, where no workflow execution is required.
    Use this after the entrypoint URL is known and before authoring blocks that
    fill fields, submit searches, filter results, or expand result rows. It
    can also inspect the current browser page after a run by passing
    target_url="current_page". `target="debug"` (the default) keeps the read on
    the browser this chat drives; `target="last_run"` reads the browser used by
    the most recent test run. Passing any other
    `target_url` navigates the targeted browser there and reports the reached
    `current_url`, so a further `navigate_browser` to that same URL is redundant. Navigating
    `target="last_run"` leaves the page that run stopped on, losing the state you are diagnosing,
    and anything it submits there is real; pass `target_url="current_page"` to observe that browser
    without moving it. The packet
    describes the page only as it is at that moment.

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
    the user did not supply enough information for, ask the user for that observed missing input. If
    evidence is sufficient, compose and run workflow blocks from the observed fields.
    `challenge_state` reports what the page looks like, which is not what a run will do:
    it does not establish that a submit/search path is closed, and a run settles that.

    When the page visibly shows a requested output but its markup is unclear, pass
    `requested_output_reads` with the `output_path` your block will return, the exact
    rendered `value_text`, and its visible `label`. The browser verifies the designation
    and returns every observed selector candidate as facts; you remain responsible for
    choosing a selector and authoring the workflow read.
    """
    copilot_ctx = ctx.context
    target_value = target.value
    arguments: dict[str, Any] = {"target_url": target_url, "target": target_value}
    if requested_output_reads is not None:
        arguments["requested_output_reads"] = requested_output_reads
    binding = resolve_browser_session_binding(copilot_ctx, {"target": target_value})

    def finish(result: dict[str, Any], source_browser_session_id: str | None) -> str:
        result_data = result.get("data")
        session_provenance = result_data.get("browser_session_provenance") if isinstance(result_data, dict) else None
        mixed_session_provenance = isinstance(session_provenance, dict) and session_provenance.get("mixed") is True
        source_matches_target = (
            binding.source_matches_target
            and not mixed_session_provenance
            and source_browser_session_id is not None
            and source_browser_session_id == binding.session_id_for(copilot_ctx)
        )
        stamped = {
            **result,
            **binding.provenance(),
            "source_matches_target": source_matches_target,
            "source_browser_session_id": source_browser_session_id,
        }
        scrubbed = scrub_secrets_from_structure(copilot_ctx, stamped)
        model_result = _model_facing_inspect_result(scrubbed, copilot_ctx=copilot_ctx)
        record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, model_result)
        return json.dumps(model_result)

    if binding.unavailable_reason:
        return finish(
            {"ok": False, "data": None, "error": binding.unavailable_reason},
            None,
        )

    with bound_call_browser_session(binding.session_id_override):
        authority_error = _authority_tool_error(copilot_ctx, "inspect_page_for_composition")
        if authority_error:
            authority_result = json.loads(
                _diagnosis_repair_tool_error(copilot_ctx, "inspect_page_for_composition", authority_error)
            )
            return finish(
                authority_result,
                None,
            )
        admitted, _ = admitted_requested_output_reads(requested_output_reads or [])
        result = await _inspect_page_for_composition_impl(copilot_ctx, target_url, admitted)
        if requested_output_reads and result.get("ok"):
            data = result.get("data")
            witnessed_paths = _witnessed_output_paths(data)
            verified, unverified = await _verify_requested_output_reads(
                copilot_ctx, requested_output_reads, witnessed_paths
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
            requested_paths = requested_output_paths_for_derivation(copilot_ctx)
            data = result.get("data")
            if requested_paths and isinstance(data, dict):
                data["requested_output_designation_capability"] = requested_output_designation_capability(
                    list(requested_paths)
                )
        data = result.get("data")
        source_browser_session_id = (
            data.get("source_browser_session_id")
            if isinstance(data, dict) and "source_browser_session_id" in data
            else None
        )
        if not isinstance(source_browser_session_id, str):
            source_browser_session_id = None
        return finish(result, source_browser_session_id)


@function_tool(failure_error_function=copilot_tool_failure, name_override="fill_credential_field", strict_mode=False)
async def fill_credential_field_tool(
    ctx: RunContextWrapper,
    selector: str,
    credential_id: str,
    field: str,
    submit_selector: str | None = None,
    target: BrowserTarget = BrowserTarget.DEBUG,
) -> str:
    """Fill ONE field of a SAVED credential into a live browser during code-only scouting.

    `target="debug"` (the default) fills the browser this chat drives; `target="last_run"` fills the
    browser the most recent test run executed in.

    The secret value is resolved server-side from the stored credential and never
    enters the conversation; the result reports only `typed_length`. Use this
    rather than typing the value yourself whenever a login form field should receive a saved
    credential's username, password, or authenticator-app one-time code. Email/SMS
    OTP credentials are not filled during scouting because scouting has no
    workflow run/task context for safe polling.

    The result's `readback_outcome` reports what the field held right after the fill —
    `exact_match`, `different` (the field holds something other than what was typed),
    `empty`, or `unavailable` (the field could not be read) — and only `empty` fails
    the fill; decide from that outcome whether the page still needs another action.
    An `empty` readback still succeeds when `landing_inferred_from_navigation` is true:
    the page left the one the fill acted on, so the field was cleared by its own submit.

    To test an existing saved login block, run that block unchanged.
    When repairing its login in the live browser, reuse the saved block's credential here;
    its saved login origin, tested site, or vault site can authorize the fill without another ask.

    `selector` must be a CSS selector for the exact input field (no comma-union
    fallbacks — inspect the page first and target the proven field).
    `credential_id`: when a page observation returns
    `resolved_login_credential_id`, the server has already authorized that
    credential for this login page — pass that id. When it returns
    `candidate_login_credentials`, reuse an existing user or saved-workflow choice if present;
    otherwise call `request_credential` for the sign-in URL and pass the selected `credential_id`.
    `field` is one of `username`, `password`, `totp`.
    If a `totp` fill reports the credential has no authenticator, call `request_credential` with that
    `credential_id` so the user can add one.

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
    submitting on a later turn can send an already-expired code.
    This tool's own `form_submit_controls` is reported only when
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
    for that mailbox. A code block that signs in should, after submitting, give the sign-in or
    one-time-code form a bounded chance to go away (a submit that stays on the same page answers late),
    then raise with what the page shows if the form is still there, so a refused password or code fails
    the run instead of passing it.
    """
    binding = resolve_browser_session_binding(ctx.context, {"target": target.value})
    if binding.unavailable_reason:
        # Never fall back to the chat's browser: a credential filled there lands on a page the model did not name.
        return json.dumps({"ok": False, "error": binding.unavailable_reason, **binding.provenance()})
    with bound_call_browser_session(binding.session_id_override):
        result = await _fill_credential_field_impl(ctx.context, selector, credential_id, field, submit_selector)
    return json.dumps(scrub_secrets_from_structure(ctx.context, {**result, **binding.provenance()}))


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


ACCOUNT_GROUP_TOOL_NAMES = frozenset(
    {ACCOUNT_GROUP_SUBMIT_TOOL_NAME, ACCOUNT_GROUP_STATUS_TOOL_NAME, ACCOUNT_GROUP_CANCEL_TOOL_NAME}
)

NATIVE_TOOLS = [
    ask_user_tool,
    set_work_plan_tool,
    update_workflow_tool,
    edit_block_tool,
    edit_block_and_run_tool,
    add_block_tool,
    delete_block_tool,
    list_credentials_tool,
    list_integrations_tool,
    read_google_sheet_tool,
    get_organization_usage_quota_tool,
    run_blocks_tool,
    test_workflow_from_blank_browser_tool,
    get_run_results_tool,
    update_and_run_blocks_tool,
    discover_workflow_entrypoint_tool,
    search_web_tool,
    inspect_page_for_composition_tool,
    inspect_locator_matches_tool,
    fill_credential_field_tool,
    request_credential_tool,
    run_browser_code_tool,
    solve_page_challenge_tool,
    start_fresh_browser_tool,
    extend_browser_session_tool,
    upload_attached_file_tool,
    run_workflow_for_accounts_tool,
    get_account_group_status_tool,
    cancel_account_group_tool,
    delete_saved_credentials_tool,
]


# Not advertised without browser authority: these drive a run, the scouting tab or a live page.
# Listed by hand; FunctionTool has no capability metadata.
BROWSER_BOUND_TOOL_NAMES = BLOCK_RUNNING_TOOLS | frozenset(
    {
        "discover_workflow_entrypoint",
        "inspect_page_for_composition",
        LOCATOR_INSPECTION_TOOL_NAME,
        "fill_credential_field",
        BROWSER_CODE_TOOL_NAME,
        SOLVE_TOOL_NAME,
        FRESH_BROWSER_TOOL_NAME,
        SESSION_EXTENSION_TOOL_NAME,
        UPLOAD_TOOL_NAME,
        ACCOUNT_GROUP_SUBMIT_TOOL_NAME,
    }
)


AUTHORING_GUIDANCE_TOOL_NAMES = frozenset({"add_block", "update_workflow", "update_and_run_blocks"})
_PAGE_STATE_TOOL_NAMES = (
    BROWSER_BOUND_TOOL_NAMES
    - BLOCK_RUNNING_TOOLS
    - {
        SESSION_EXTENSION_TOOL_NAME,
        ACCOUNT_GROUP_SUBMIT_TOOL_NAME,
    }
)


def _with_page_state(tool: FunctionTool) -> FunctionTool:
    """Stamp the page the call's browser shows once the tool returns onto its JSON object result."""
    invoke = tool.on_invoke_tool
    properties = tool.params_json_schema.get("properties")
    accepts_target = isinstance(properties, dict) and BROWSER_TARGET_PARAM_NAME in properties

    # Annotated ToolContext for the same reason as current_page_inspection_tool: the runner forks a bare
    # context for a RunContextWrapper annotation, and the delegate reads tool_name off it.
    async def invoke_with_page_state(ctx: ToolContext[CopilotContext], arguments: str) -> Any:
        copilot_ctx = ctx.context
        target = None
        if accepts_target:
            try:
                parsed = json.loads(arguments) if arguments else {}
            except ValueError:
                parsed = {}
            target = parsed.get(BROWSER_TARGET_PARAM_NAME) if isinstance(parsed, dict) else None
        # Resolved before the tool runs and with no await in between, so the read lands on the browser the
        # tool's own resolution chose.
        binding = resolve_browser_session_binding(copilot_ctx, {BROWSER_TARGET_PARAM_NAME: target})
        with bound_call_browser_session(binding.session_id_override):
            output = await invoke(ctx, arguments)
            try:
                result = json.loads(output) if isinstance(output, str) else None
            except ValueError:
                return output
            # An empty result is the fail-closed answer, and a stamp would turn it into a successful call.
            if not isinstance(result, dict) or not result or "page_state" in result:
                return output
            page_state = await read_page_state(
                copilot_ctx,
                tool_name=tool.name,
                result=result,
                binding=binding,
                custody_lock=browser_page_custody_lock(copilot_ctx, session_id=binding.session_id_for(copilot_ctx)),
            )
        return json.dumps({"page_state": page_state, **result})

    return dataclasses.replace(tool, on_invoke_tool=invoke_with_page_state)


def _with_action_reason(tool: FunctionTool) -> FunctionTool:
    schema = copy.deepcopy(tool.params_json_schema)
    schema.setdefault("properties", {})[USER_FACING_REASON_PARAM] = dict(USER_FACING_REASON_SCHEMA)

    async def invoke(ctx: ToolContext[CopilotContext], arguments: str) -> Any:
        with capturing_tool_call(_originating_call_id(ctx)):
            try:
                ordinary = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                return await tool.on_invoke_tool(ctx, arguments)
            if isinstance(ordinary, dict):
                ordinary.pop(USER_FACING_REASON_PARAM, None)
                arguments = json.dumps(ordinary)
            return await tool.on_invoke_tool(ctx, arguments)

    return dataclasses.replace(tool, params_json_schema=schema, strict_json_schema=False, on_invoke_tool=invoke)


# Each parameter takes a reference only the browser-code tool produces, so a surface without that tool
# advertises neither the parameter nor its text.
_EXECUTED_SOURCE_PARAMS = {
    "edit_block_and_run": (
        "executed_source_reference",
        (
            f"The `executed_source_reference` of a `{BROWSER_CODE_TOOL_NAME}` cell you already ran. That cell's "
            "complete source replaces the block's code byte-for-byte. Pass this instead of `expected_code` and "
            "`replacement_code`."
        ),
    ),
    "update_and_run_blocks": (
        "executed_source_references",
        (
            f"Saves code you already ran with `{BROWSER_CODE_TOOL_NAME}` as a new block: maps the block's label to "
            "that cell's `executed_source_reference`. Leave the block's `code` empty; the cell's exact source "
            "becomes the block's code, so what gets tested is what you ran."
        ),
    ),
}


def _with_executed_source_param(tool: FunctionTool, *, browser_code_available: bool) -> FunctionTool:
    name, description = _EXECUTED_SOURCE_PARAMS[tool.name]
    schema = copy.deepcopy(tool.params_json_schema)
    if browser_code_available:
        schema["properties"][name]["description"] = description
    else:
        del schema["properties"][name]
    return dataclasses.replace(tool, params_json_schema=schema)


_LIST_INTEGRATIONS_RUN_GUIDANCE = (
    "For a build-and-test request, pass the completed workflow and the bound block label to "
    f"`{UPDATE_AND_RUN_BLOCKS_TOOL_NAME}` in the same turn; do not stop at `{UPDATE_WORKFLOW_TOOL_NAME}`."
)


def copilot_native_tools(
    *,
    supports_question_tool: bool,
    browser_code_available: bool,
    authoring_capability: AuthoringCapability | BlockAuthoringPolicy | str | None = None,
    run_tools_available: bool,
    supports_account_group_card: bool = False,
    supports_credential_delete_card: bool = False,
) -> list[FunctionTool]:
    capability = _normalized_authoring_capability(authoring_capability)
    both_families = capability.code_blocks and capability.agent_blocks
    appended = SCHEMA_FIRST_GUIDANCE if not both_families else f"{AUTHORING_FAMILY_GUIDANCE}\n\n{SCHEMA_FIRST_GUIDANCE}"
    tools: list[FunctionTool] = []
    for tool in NATIVE_TOOLS:
        if (
            (tool.name == "ask_user" and not supports_question_tool)
            or (tool.name == BROWSER_CODE_TOOL_NAME and not browser_code_available)
            or (tool.name in ACCOUNT_GROUP_TOOL_NAMES and not supports_account_group_card)
            or (tool.name == ACCOUNT_GROUP_SUBMIT_TOOL_NAME and not account_group_submit_enabled())
            or (
                tool.name == CREDENTIAL_DELETE_TOOL_NAME
                and not (supports_credential_delete_card and credential_delete_enabled())
            )
        ):
            continue
        if tool.name in _EXECUTED_SOURCE_PARAMS:
            tool = _with_executed_source_param(tool, browser_code_available=browser_code_available)
        if tool.name == list_integrations_tool.name and run_tools_available:
            tool = dataclasses.replace(tool, description=f"{tool.description}\n\n{_LIST_INTEGRATIONS_RUN_GUIDANCE}")
        if tool.name in AUTHORING_GUIDANCE_TOOL_NAMES:
            tool = dataclasses.replace(tool, description=f"{tool.description}\n\n{appended}")
        elif tool.name in _PAGE_STATE_TOOL_NAMES:
            tool = _with_page_state(tool)
        tools.append(_with_action_reason(tool))
    return tools

"""Copilot agent tools — native handlers, hooks, and registration."""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from agents import function_tool
from agents.run_context import RunContextWrapper

from skyvern.forge import app as app
from skyvern.forge.sdk.copilot.build_phase import (
    advance_to_testing,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    composition_page_evidence_error as composition_page_evidence_error,
)
from skyvern.forge.sdk.copilot.composition_evidence import has_bounded_page_schema as has_bounded_page_schema
from skyvern.forge.sdk.copilot.composition_evidence import (
    normalize_block_observation_refs,
)
from skyvern.forge.sdk.copilot.composition_evidence import workflow_target_url as workflow_target_url
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.loop_detection import record_tool_step_result_for_ctx
from skyvern.forge.sdk.copilot.output_utils import (
    _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY as _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
)
from skyvern.forge.sdk.copilot.output_utils import (
    sanitize_tool_result_for_llm,
)
from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.workflow_yaml import BlockEditError
from skyvern.forge.sdk.copilot.workflow_yaml import _process_workflow_yaml as _process_workflow_yaml
from skyvern.forge.sdk.copilot.workflow_yaml import apply_block_edit, delete_block_from_workflow
from skyvern.utils.yaml_loader import safe_load_no_dates

from ._shared import _COMPOSITION_STRIPPED_HTML_MAX_CHARS as _COMPOSITION_STRIPPED_HTML_MAX_CHARS
from ._shared import _FAILED_BLOCK_STATUSES as _FAILED_BLOCK_STATUSES
from ._shared import BLOCK_RUNNING_TOOLS as BLOCK_RUNNING_TOOLS
from ._shared import COPILOT_FINAL_REPLY_RESERVE_SECONDS as COPILOT_FINAL_REPLY_RESERVE_SECONDS
from ._shared import PER_TOOL_CALL_BUDGET_SECONDS as PER_TOOL_CALL_BUDGET_SECONDS
from ._shared import (
    RUN_BLOCKS_SAFETY_CEILING_SECONDS,
)
from ._shared import _composition_get_html as _composition_get_html
from ._shared import _current_workflow_has_evidence_block as _current_workflow_has_evidence_block
from ._shared import _fallback_page_info as _fallback_page_info
from ._shared import _is_meaningful_extracted_data as _is_meaningful_extracted_data
from ._shared import _proxy_location_trace_value as _proxy_location_trace_value
from ._shared import _raw_yaml_proxy_location as _raw_yaml_proxy_location
from ._shared import _same_page_ignoring_fragment as _same_page_ignoring_fragment
from ._shared import _unverified_current_workflow_labels as _unverified_current_workflow_labels
from .banned_blocks import _COPILOT_BANNED_BLOCK_TYPES as _COPILOT_BANNED_BLOCK_TYPES
from .banned_blocks import _banned_block_reject_message as _banned_block_reject_message
from .banned_blocks import _detect_new_banned_blocks as _detect_new_banned_blocks
from .banned_blocks import _record_banned_block_reject_span as _record_banned_block_reject_span
from .blockers import _active_block_run_budget_seconds as _active_block_run_budget_seconds
from .blockers import _analyze_run_blocks as _analyze_run_blocks
from .blockers import _build_loop_blocker_signal as _build_loop_blocker_signal
from .blockers import _last_run_has_terminal_anti_bot_blocker as _last_run_has_terminal_anti_bot_blocker
from .blockers import _per_tool_budget_problem_rerun_signal as _per_tool_budget_problem_rerun_signal
from .blockers import _post_budget_terminal_challenge_signal as _post_budget_terminal_challenge_signal
from .blockers import _post_run_terminal_challenge_reason as _post_run_terminal_challenge_reason
from .blockers import (
    _record_per_tool_budget_problem_blocks_from_results,
)
from .blockers import _run_blocks_structured_blocker_message as _run_blocks_structured_blocker_message
from .blockers import (
    _tool_loop_error,
)
from .blockers import _trusted_post_drain_status as _trusted_post_drain_status
from .completion import _build_run_evidence_snapshot as _build_run_evidence_snapshot
from .completion import _completion_verification_handler as _completion_verification_handler
from .completion import _is_outcome_evidence_candidate as _is_outcome_evidence_candidate
from .completion import _is_unfinished_run_verification_candidate as _is_unfinished_run_verification_candidate
from .completion import _maybe_run_completion_verification as _maybe_run_completion_verification
from .completion import (
    _maybe_run_completion_verification_from_page_observation as _maybe_run_completion_verification_from_page_observation,
)
from .completion import _outcome_failure_warrants_repair as _outcome_failure_warrants_repair
from .completion import _outcome_unverified_reason as _outcome_unverified_reason
from .completion import (
    _tool_visible_result_after_completion_verification,
)
from .composition_capture import _capture_composition_evidence as _capture_composition_evidence
from .composition_capture import (
    _composition_evidence_after_navigation_failure as _composition_evidence_after_navigation_failure,
)
from .composition_capture import _composition_visual_handler as _composition_visual_handler
from .composition_capture import _composition_visual_prompt as _composition_visual_prompt
from .composition_capture import (
    _inspect_page_for_composition_impl,
)
from .composition_capture import _normalized_inspect_url as _normalized_inspect_url
from .composition_capture import _same_inspect_target as _same_inspect_target
from .credential_fill import _credential_fill_policy_error as _credential_fill_policy_error
from .credential_fill import (
    _fill_credential_field_impl,
)
from .credential_fill import _resolve_credential_fill_value as _resolve_credential_fill_value
from .credentials import _credential_id_misbinding_error_message as _credential_id_misbinding_error_message
from .credentials import _credential_id_misbinding_findings as _credential_id_misbinding_findings
from .credentials import _credential_reference_validation_error as _credential_reference_validation_error
from .credentials import _extract_credential_ids_from_tool_value as _extract_credential_ids_from_tool_value
from .credentials import _extract_credential_ids_from_workflow_yaml as _extract_credential_ids_from_workflow_yaml
from .credentials import (
    _list_credentials,
)
from .discovery import _DISCOVERY_NAVIGATION_FALLBACK_CONFIDENCE as _DISCOVERY_NAVIGATION_FALLBACK_CONFIDENCE
from .discovery import _DISCOVERY_PER_CHAT_BUDGET as _DISCOVERY_PER_CHAT_BUDGET
from .discovery import _DISCOVERY_PER_TURN_BUDGET as _DISCOVERY_PER_TURN_BUDGET
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
from .frontier import _frontier_run_size_error as _frontier_run_size_error
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
)
from .guardrails import _parameter_binding_invariant_error as _parameter_binding_invariant_error
from .guardrails import (
    _request_policy_allows_credential_deferred_draft,
    _request_policy_allows_update_and_skip_run,
)
from .guardrails import _turn_intent_tool_error as _turn_intent_tool_error
from .mcp_hooks import _build_skyvern_mcp_overlays as _build_skyvern_mcp_overlays
from .mcp_hooks import _click_post_hook as _click_post_hook
from .mcp_hooks import _click_pre_hook as _click_pre_hook
from .mcp_hooks import (
    _code_only_pre_run_results_error,
    _demonstrated_step_sources,
)
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
from .run_execution import _cancel_run_task_if_not_final as _cancel_run_task_if_not_final
from .run_execution import _composition_anti_bot_reason as _composition_anti_bot_reason
from .run_execution import _detect_non_retriable_nav_error as _detect_non_retriable_nav_error
from .run_execution import (
    _diagnosis_repair_tool_error,
    _frontier_run_size_result,
    _get_run_results,
)
from .run_execution import _mark_pending_reconciliation_run as _mark_pending_reconciliation_run
from .run_execution import (
    _maybe_clear_reconciliation_flag,
)
from .run_execution import _progress_marker as _progress_marker
from .run_execution import _read_progress_sources as _read_progress_sources
from .run_execution import (
    _record_diagnosis_repair_contract,
)
from .run_execution import _record_run_blocks_result as _record_run_blocks_result
from .run_execution import (
    _run_blocks_and_collect_debug,
    _run_blocks_span_data,
    _verify_and_record_run_blocks_result,
)
from .run_execution import _watchdog_error_message as _watchdog_error_message
from .run_execution import _watchdog_user_failure_reason as _watchdog_user_failure_reason
from .scouting import _MAX_SCOUTED_INTERACTIONS as _MAX_SCOUTED_INTERACTIONS
from .scouting import _capture_accessible_role_name as _capture_accessible_role_name
from .scouting import _capture_scout_ambiguity as _capture_scout_ambiguity
from .scouting import _capture_scout_role_name as _capture_scout_role_name
from .scouting import _capture_scout_source_url as _capture_scout_source_url
from .scouting import _clear_pending_browser_interaction_observation as _clear_pending_browser_interaction_observation
from .scouting import (
    _consume_pending_browser_interaction_observation as _consume_pending_browser_interaction_observation,
)
from .scouting import _consume_scout_source_url as _consume_scout_source_url
from .scouting import _mark_page_inspected as _mark_page_inspected
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
from .workflow_update import (
    _impose_output_contract_envelope_after_steering as _impose_output_contract_envelope_after_steering,
)
from .workflow_update import _normalize_code_artifact_metadata as _normalize_code_artifact_metadata
from .workflow_update import _record_workflow_proxy_location_span as _record_workflow_proxy_location_span
from .workflow_update import _record_workflow_update_result as _record_workflow_update_result
from .workflow_update import _scaffold_metadata_contract_for_update as _scaffold_metadata_contract_for_update
from .workflow_update import _update_workflow as _update_workflow
from .workflow_update import carry_author_time_findings as carry_author_time_findings
from .workflow_update import (
    consume_output_contract_advisory_grant_for_run_result as consume_output_contract_advisory_grant_for_run_result,
)

LOG = structlog.get_logger()

_CREDENTIAL_DEFERRED_DRAFT_MESSAGE = (
    "I can save this as a draft without running it because the credentials aren't set up yet. "
    "Add them in the Credentials UI and ask me to test the workflow."
)


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
    loop_error = _tool_loop_error(copilot_ctx, "update_workflow", arguments)
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})
    credential_deferred_draft = _request_policy_allows_credential_deferred_draft(copilot_ctx)

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
        )
        if credential_deferred_draft and result.get("ok"):
            _mark_credential_deferred_draft(copilot_ctx, result)
        _record_workflow_update_result(copilot_ctx, result, prior_definition)
        record_tool_step_result_for_ctx(copilot_ctx, "update_workflow", arguments, result)
        if result.get("ok") is False:
            _record_diagnosis_repair_contract(
                copilot_ctx,
                source_tool="update_workflow",
                result=result,
            )
    sanitized = sanitize_tool_result_for_llm("update_workflow", result)
    return json.dumps(sanitized)


async def _persist_block_scoped_edit(copilot_ctx: Any, tool_name: str, workflow_yaml: str, arguments: dict) -> str:
    """Send a server-composed workflow through the normal persistence path.

    The model never sends the whole workflow, but the saved result still goes through every
    author-time check, so a block edit cannot slip past what a full submission must satisfy.
    """
    prior_definition = await _get_prior_workflow_definition(copilot_ctx)
    with copilot_span(tool_name, data={"yaml_length": len(workflow_yaml)}):
        result = await _update_workflow({"workflow_yaml": workflow_yaml}, copilot_ctx)
        _record_workflow_update_result(copilot_ctx, result, prior_definition)
        record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, result)
        if result.get("ok") is False:
            _record_diagnosis_repair_contract(copilot_ctx, source_tool=tool_name, result=result)
    return json.dumps(sanitize_tool_result_for_llm(tool_name, result))


def _stored_workflow_yaml(copilot_ctx: Any) -> str:
    latest = getattr(copilot_ctx, "last_workflow_yaml", None)
    if isinstance(latest, str) and latest.strip():
        return latest
    stored = getattr(copilot_ctx, "workflow_yaml", None)
    return stored if isinstance(stored, str) else ""


def _submission_only_changes_existing_blocks(copilot_ctx: Any, workflow_yaml: str) -> bool:
    """Whether a whole-workflow submission changes nothing structural.

    Same labels in, same labels out means the model is editing block contents through a tool that
    re-sends every block. Adding or removing a block is structural and still needs the whole
    workflow, since edit_block only reaches blocks that already exist.
    """
    stored = _stored_workflow_yaml(copilot_ctx)
    if not stored.strip() or not workflow_yaml.strip():
        return False
    try:
        before = safe_load_no_dates(stored)
        after = safe_load_no_dates(workflow_yaml)
    except Exception:
        return False

    def labels(parsed: Any) -> set[str] | None:
        if not isinstance(parsed, dict):
            return None
        definition = parsed.get("workflow_definition")
        if not isinstance(definition, dict):
            return None
        blocks = definition.get("blocks")
        if not isinstance(blocks, list):
            return None
        return {str(b.get("label") or "") for b in blocks if isinstance(b, dict)}

    before_labels, after_labels = labels(before), labels(after)
    if before_labels is None or after_labels is None or not after_labels:
        return False
    return before_labels == after_labels


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
    loop_error = _tool_loop_error(copilot_ctx, "edit_block", arguments)
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})
    authority_error = _authority_tool_error(copilot_ctx, "edit_block")
    if authority_error:
        return json.dumps({"ok": False, "error": authority_error})
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
        return json.dumps(result)
    return await _persist_block_scoped_edit(copilot_ctx, "edit_block", workflow_yaml, arguments)


@function_tool(name_override="delete_block")
async def delete_block_tool(ctx: RunContextWrapper, label: str) -> str:
    """Remove one block from the workflow by label.

    Deleting is an explicit action: leaving a block out of a workflow you send elsewhere does not
    remove it. Any block that pointed at this one as its next step is unlinked.
    """
    copilot_ctx = ctx.context
    arguments = {"label": label}
    loop_error = _tool_loop_error(copilot_ctx, "delete_block", arguments)
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})
    authority_error = _authority_tool_error(copilot_ctx, "delete_block")
    if authority_error:
        return json.dumps({"ok": False, "error": authority_error})
    try:
        workflow_yaml = delete_block_from_workflow(_stored_workflow_yaml(copilot_ctx), label)
    except BlockEditError as exc:
        result = {"ok": False, "error": str(exc)}
        record_tool_step_result_for_ctx(copilot_ctx, "delete_block", arguments, result)
        return json.dumps(result)
    return await _persist_block_scoped_edit(copilot_ctx, "delete_block", workflow_yaml, arguments)


@function_tool(name_override="synthesize_demonstrated_block")
async def synthesize_demonstrated_block_tool(ctx: RunContextWrapper) -> str:
    """Return code-block source built from the steps you already performed in the browser.

    This is compiled from your recorded interactions, so it reflects what actually happened rather
    than what you remember doing. Use it as the body of a code block instead of writing the steps
    again from memory, and edit it if the block needs to do more.
    """
    copilot_ctx = ctx.context
    arguments: dict[str, Any] = {}
    loop_error = _tool_loop_error(copilot_ctx, "synthesize_demonstrated_block", arguments)
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})
    source = _demonstrated_step_sources(copilot_ctx)
    if not source:
        result = {
            "ok": False,
            "error": "Nothing has been demonstrated in the browser yet, so there are no steps to compile.",
        }
    else:
        result = {"ok": True, "data": {"code": source}}
    record_tool_step_result_for_ctx(copilot_ctx, "synthesize_demonstrated_block", arguments, result)
    return json.dumps(result)


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
        consume_output_contract_advisory_grant_for_run_result(copilot_ctx, result)
        completion_verification = await _verify_and_record_run_blocks_result(copilot_ctx, result, handler_start)
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
    if _submission_only_changes_existing_blocks(copilot_ctx, workflow_yaml):
        # Re-sending every block to change some of them is what disturbs a block that already works
        # and grows the turn with each repair. Structural changes still come through here.
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "This submission changes existing blocks without adding or removing any, so send just "
                    "the change: call edit_block once per block you are actually changing (or delete_block "
                    "to remove one), then run the affected blocks with run_blocks_and_collect_debug. Send a "
                    "whole workflow here only when the set of blocks itself changes."
                ),
            }
        )
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
    skip_run_after_update = _request_policy_allows_update_and_skip_run(copilot_ctx, "update_and_run_blocks")
    # Cleared unconditionally up front and only set True at the actual skip
    # branch below — reflects "we skipped a run", not "the policy would have
    # allowed a skip if we got that far". A stale True from an earlier call, or
    # a premature True from a policy check ahead of an unrelated update_workflow
    # failure, would misreport an authoring error as a credential ask.
    copilot_ctx.last_run_skipped_unbound_credentials = False
    authority_error = _authority_tool_error(
        copilot_ctx,
        "update_and_run_blocks",
        ignore_request_policy_error=skip_run_after_update,
    )
    if authority_error:
        return _diagnosis_repair_tool_error(copilot_ctx, "update_and_run_blocks", authority_error)

    workflow_yaml, imposed_code_artifact_metadata, envelope_imposed = _impose_output_contract_envelope_after_steering(
        copilot_ctx,
        workflow_yaml,
        serialized_code_artifact_metadata,
    )
    if envelope_imposed:
        serialized_code_artifact_metadata = imposed_code_artifact_metadata
        arguments["workflow_yaml"] = workflow_yaml
        arguments["code_artifact_metadata"] = serialized_code_artifact_metadata

    scaffolded_code_artifact_metadata, scaffold_applied = _scaffold_metadata_contract_for_update(
        copilot_ctx,
        workflow_yaml,
        serialized_code_artifact_metadata,
    )
    if scaffold_applied:
        serialized_code_artifact_metadata = scaffolded_code_artifact_metadata
        arguments["code_artifact_metadata"] = serialized_code_artifact_metadata

    loop_error = _tool_loop_error(copilot_ctx, "update_and_run_blocks", arguments)
    if loop_error:
        return _diagnosis_repair_tool_error(copilot_ctx, "update_and_run_blocks", loop_error)

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
                "raw_code_artifact_metadata": serialized_code_artifact_metadata
                if scaffold_applied or envelope_imposed
                else code_artifact_metadata,
                "block_labels": block_labels,
                "parameters": parameters or {},
            },
            copilot_ctx,
            allow_missing_credentials=skip_run_after_update,
            allow_static_output_uncertainty=True,
            formation_prepared=True,
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

    if skip_run_after_update:
        copilot_ctx.last_run_skipped_unbound_credentials = True
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
        carry_author_time_findings(update_result, skip_result)
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
        carry_author_time_findings(update_result, result)
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
        consume_output_contract_advisory_grant_for_run_result(copilot_ctx, run_result)
        completion_verification = await _verify_and_record_run_blocks_result(copilot_ctx, run_result, handler_start)
        tool_visible_result = _tool_visible_result_after_completion_verification(
            copilot_ctx,
            run_result,
            completion_verification,
        )
        carry_author_time_findings(update_result, tool_visible_result)
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

    Budget: one successful call per turn, three per chat, eight page hops,
    sixty seconds. On any ``failure_reason``, ASK_QUESTION for a URL — do not
    retry. Discovery navigates and reads pages; it will NOT type, click form
    buttons, run JavaScript, or submit forms.
    """
    result = await _discover_workflow_entrypoint_impl(ctx.context, site_or_url, intent_hint)
    return json.dumps(scrub_secrets_from_structure(ctx.context, result))


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
    return json.dumps(scrub_secrets_from_structure(ctx.context, result))


@function_tool(name_override="fill_credential_field", strict_mode=False)
async def fill_credential_field_tool(
    ctx: RunContextWrapper,
    selector: str,
    credential_id: str,
    field: str,
) -> str:
    """Fill ONE field of a SAVED credential into the live debug browser during code-only scouting.

    The secret value is resolved server-side from the stored credential and never
    enters the conversation; the result reports only `typed_length`. Use this
    instead of `type_text` whenever a login form field should receive a saved
    credential's username, password, or authenticator-app one-time code. Email/SMS
    OTP credentials are not filled during scouting because scouting has no
    workflow run/task context for safe polling.

    `selector` must be a CSS selector for the exact input field (no comma-union
    fallbacks — inspect the page first and target the proven field).
    `credential_id` must be a credential from the request policy's
    `resolved_credentials`. `field` is one of `username`, `password`, `totp`.

    This tool only fills; it never clicks or submits. Each successful fill is
    recorded as a scouted interaction, so the SYNTHESIZED CODE BLOCK will bind
    the credential as a `credential_id` workflow parameter and reference
    username/password as `<parameter_key>.username` / `.password`.

    In synthesized code blocks, one-time codes must use
    `await <parameter_key>.otp()` so authenticator, email, and SMS OTP sources
    all resolve at runtime.
    """
    result = await _fill_credential_field_impl(ctx.context, selector, credential_id, field)
    return json.dumps(scrub_secrets_from_structure(ctx.context, result))


NATIVE_TOOLS = [
    update_workflow_tool,
    edit_block_tool,
    delete_block_tool,
    synthesize_demonstrated_block_tool,
    list_credentials_tool,
    run_blocks_tool,
    get_run_results_tool,
    update_and_run_blocks_tool,
    discover_workflow_entrypoint_tool,
    inspect_page_for_composition_tool,
    fill_credential_field_tool,
]

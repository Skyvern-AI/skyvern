from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

import structlog
from pydantic import JsonValue

from skyvern.cli.core.js_dispatch import outer_cap_seconds
from skyvern.cli.mcp_tools._element_state import DEFAULT_ACTION_TIMEOUT_MS
from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.copilot.block_type_aliases import normalize_copilot_block_type_alias
from skyvern.forge.sdk.copilot.blocker_signal import stash_blocker_signal
from skyvern.forge.sdk.copilot.composition_browser_expressions import scout_control_state_expression
from skyvern.forge.sdk.copilot.config import (
    AuthoringCapability,
    BlockAuthoringPolicy,
    authoring_capability_from_policy,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.credential_resolution import is_resolved_page_url, load_credentials
from skyvern.forge.sdk.copilot.enforcement import (
    requested_output_paths_for_derivation,
)
from skyvern.forge.sdk.copilot.mcp_adapter import (
    BROWSER_TARGET_PARAM,
    BROWSER_TARGET_PARAM_NAME,
    PreHook,
    SchemaOverlay,
    scrub_model_facing_tool_result,
)
from skyvern.forge.sdk.copilot.output_extraction_plan import (
    requested_output_designation_capability,
    unbound_candidate_relations,
)
from skyvern.forge.sdk.copilot.output_utils import (
    mark_mcp_result_untrusted_for_llm,
    sanitize_tool_result_for_llm,
)
from skyvern.forge.sdk.copilot.page_identity import safe_page_origin
from skyvern.forge.sdk.copilot.reached_download_target import download_claim_helper_contract
from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    live_page_credentials_admissible,
    resolve_credential_for_live_page,
)
from skyvern.forge.sdk.copilot.request_slots import is_canonical_request_slot_path
from skyvern.forge.sdk.copilot.runtime import (
    SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR,
    SENSITIVE_ORIGIN_PAGE_ERROR,
    AgentContext,
    ScoutedInteraction,
    ScoutedSelectorCandidate,
    browser_valid_tab_count,
    clear_sensitive_origin_page_taint_after_navigation,
    live_working_page_url,
    navigation_replaced_document,
    pending_taint_source_url,
    sensitive_origin_multi_tab_error,
    sensitive_origin_page_facts_withheld,
    sensitive_origin_page_has_active_run,
    sensitive_origin_page_is_tainted,
    stage_pending_taint_source,
    tab_switch_refusal,
)
from skyvern.forge.sdk.copilot.screenshot_utils import viewport_visible_effect
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.secret_scrub import (
    register_matching_origin_run_redaction_values,
    registered_scrub_values,
    scrub_secrets_from_structure,
)
from skyvern.forge.sdk.schemas.credentials import Credential
from skyvern.forge.sdk.workflow.models.block import (
    CLEAR_BROWSER_DATA_HELPER_CONTRACT,
    OPEN_PAGE_HELPER_CONTRACT,
    open_page_cap_reached,
)
from skyvern.forge.sdk.workflow.web_search import WEB_SEARCH_HELPER_CONTRACT
from skyvern.schemas.workflows import TaskBlockYAML
from skyvern.utils.secret_headers import SECRET_HEADER_MASK
from skyvern.webeye.dialog_handler import DIALOG_POLICY_HELPER_CONTRACT

from ._shared import (
    _DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
    _composition_get_structured_evidence,
    _fallback_page_info,
    attribute_navigation_failure,
)
from .banned_blocks import (
    _AGENT_FAMILY_BLOCK_TYPES,
    _CODE_ONLY_TARGET_EVIDENCE_KEYS,
    _TASK_V3_ENGINE,
    AGENT_FAMILY_BLOCK_SUMMARIES,
    AUTHORING_FAMILY_GUIDANCE,
    CODE_BLOCK_SUMMARY,
    _authoring_violation_reject_message,
    _banned_block_types_for_capability,
    _block_authoring_violations,
    _code_only_browser_schema_guidance,
    _code_only_browser_unavailable_summary,
    _copilot_authoring_capability,
    _copilot_banned_block_alternatives,
    _copilot_banned_block_types,
    _copilot_block_policy,
    _record_code_native_pending_capability,
    _render_block_policy_detail,
    workflow_run_names,
)
from .credentials import (
    _credential_run_approval_blocker_signal,
    _credential_run_approval_error,
    _extract_credential_ids_from_tool_value,
    _extract_credential_ids_from_workflow_definition,
    _google_connection_reference_ids,
    _server_verified_google_account_choices,
)
from .page_observation import (
    _record_composition_page_observation,
    _resolve_url_title,
)
from .scouting import (
    _SCOUT_RESULT_CHAR_CAP,
    _arm_scout_challenge_listener,
    _arm_scout_download_listener,
    _arm_scout_popup_listener,
    _attach_evaluate_page_facts,
    _attach_scout_observation_step,
    _attach_scout_page_summary,
    _capture_post_interaction_screenshot,
    _capture_scout_pre_action,
    _capture_scout_source_url,
    _clear_pending_browser_interaction_observation,
    _clear_pending_scout_selector_facts,
    _close_scout_challenge_baseline,
    _consume_scout_source_url,
    _mark_pending_browser_interaction_observation,
    _maybe_attach_observed_challenge,
    _maybe_attach_observed_download_target,
    _maybe_attach_observed_render_target,
    _page_evidence_location_fingerprint,
    _page_evidence_names_obstruction,
    _prenav_ambiguity_for_selector,
    _prenav_role_name_for_selector,
    _record_scout_trajectory_fact,
    _record_scouted_interaction,
    _register_scout_interaction_observation,
    _release_scout_challenge_listeners,
    _resolve_scout_role_name,
    _scout_act_observe_page_evidence,
    _scout_session_download_names,
    _shed_scout_page_summary_section,
    _start_scout_challenge_settle,
    _take_viewport_frame,
    attach_navigation_challenge_vendor,
    record_signed_out_page_observation,
)

LOG = structlog.get_logger()


def _sensitive_origin_page_refusal(ctx: AgentContext) -> dict[str, Any] | None:
    if not sensitive_origin_page_is_tainted(ctx):
        return None
    return {"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR}


def _sensitive_origin_page_action_refusal(ctx: AgentContext) -> dict[str, Any] | None:
    """Refuse an action only while the page's facts cannot be scrubbed on the terminal-run route."""
    if not sensitive_origin_page_facts_withheld(ctx, getattr(ctx, "last_run_blocks_workflow_run_id", None)):
        return None
    return _sensitive_origin_page_refusal(ctx)


async def _sensitive_origin_page_pre_hook(
    _params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    return _sensitive_origin_page_refusal(ctx)


async def _tab_new_pre_hook(
    _params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    refusal = _sensitive_origin_page_refusal(ctx)
    if refusal is not None:
        return refusal
    open_tabs = await browser_valid_tab_count(ctx)
    if open_tabs is not None and open_page_cap_reached(open_tabs):
        return {
            "ok": False,
            "error": (
                f"{open_tabs} tabs are open; the browser's limit is {settings.BROWSER_MAX_PAGES_NUMBER}. "
                "Close a tab with skyvern_tab_close first."
            ),
        }
    return None


async def _sensitive_origin_page_action_pre_hook(
    _params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    return _sensitive_origin_page_action_refusal(ctx)


async def _tab_switch_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    refusal = _sensitive_origin_page_refusal(ctx)
    if refusal is not None:
        return refusal
    raw_index = params.get("index")
    index: int | None = None
    if isinstance(raw_index, int):
        index = raw_index
    elif isinstance(raw_index, str) and raw_index.isdigit():
        index = int(raw_index)
    raw_tab_id = params.get("tab_id")
    error = await tab_switch_refusal(ctx, tab_id=raw_tab_id if isinstance(raw_tab_id, str) else None, index=index)
    return {"ok": False, "error": error} if error else None


async def _tab_close_pre_hook(
    _params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    if sensitive_origin_page_has_active_run(ctx):
        return {"ok": False, "error": SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR}
    return None


async def _sensitive_origin_page_post_hook(
    result: dict[str, Any],
    _raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    return _sensitive_origin_page_refusal(ctx) or result


def _selector_from_tool_data(data: dict[str, Any], *, prefer_resolved_when_empty: bool = False) -> str:
    raw_selector = data.get("selector")
    selector = raw_selector if isinstance(raw_selector, str) else ""
    if prefer_resolved_when_empty and not selector.strip():
        resolved_selector = data.get("resolved_selector")
        selector = resolved_selector if isinstance(resolved_selector, str) else ""
    return selector.strip()


def _selector_candidates_from_tool_data(data: dict[str, Any]) -> list[ScoutedSelectorCandidate]:
    """Return every selector identity the browser tool produced, without choosing among them."""
    candidates: list[ScoutedSelectorCandidate] = []
    raw_candidates = data.get("selector_candidates")
    if isinstance(raw_candidates, list):
        for raw_candidate in raw_candidates:
            if not isinstance(raw_candidate, dict):
                continue
            selector = str(raw_candidate.get("selector") or "").strip()
            source = str(raw_candidate.get("source") or "browser").strip()
            raw_count = raw_candidate.get("match_count")
            match_count = (
                raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0 else None
            )
            if selector and not any(candidate["selector"] == selector for candidate in candidates):
                candidates.append({"selector": selector, "source": source, "match_count": match_count})
    for key, source in (("selector", "requested"), ("resolved_selector", "resolved")):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        selector = value.strip()
        if any(candidate["selector"] == selector for candidate in candidates):
            continue
        candidates.append({"selector": selector, "source": source, "match_count": None})
    return candidates


def _merge_selector_candidates(
    observed: list[ScoutedSelectorCandidate], pending: list[ScoutedSelectorCandidate] | None
) -> list[ScoutedSelectorCandidate]:
    merged = list(pending or [])
    for candidate in observed:
        existing = next((item for item in merged if item["selector"] == candidate["selector"]), None)
        if existing is None:
            merged.append(candidate)
        elif existing["match_count"] is None and candidate["match_count"] is not None:
            existing["match_count"] = candidate["match_count"]
    return merged


def _effective_target_text(selector: str, role: str = "", accessible_name: str = "") -> str:
    label = accessible_name.strip() if isinstance(accessible_name, str) else ""
    role_text = role.strip() if isinstance(role, str) else ""
    if label and role_text:
        return f"{role_text} {label}"
    return label or selector


def _failed_click_attempted_control(
    *,
    selector: str,
    selector_candidates: list[ScoutedSelectorCandidate] | None,
    selector_match_count: int | None,
    role: str,
    accessible_name: str,
    role_name_match_count: int | None,
    ambiguous: bool,
) -> dict[str, Any]:
    control: dict[str, Any] = {
        "selector": selector,
        "effective_target": _effective_target_text(selector, role, accessible_name),
    }
    if selector_candidates:
        control["selector_candidates"] = selector_candidates
    if selector_match_count is not None:
        control["selector_match_count"] = selector_match_count
    if role:
        control["role"] = role
    if accessible_name:
        control["accessible_name"] = accessible_name
    if role_name_match_count is not None:
        control["role_name_match_count"] = role_name_match_count
    if ambiguous:
        control["ambiguous"] = True
    return control


_FAILED_CLICK_ENRICHMENT_TEXT_MAX_CHARS = 160
_FAILED_CLICK_TRUNCATION_SUFFIX = "... [truncated]"


def _bound_failed_click_result(ctx: AgentContext, result: dict[str, Any]) -> None:
    """Bound the complete model-visible failure while retaining typed control/error facts."""

    scrubbed = scrub_model_facing_tool_result(ctx, result)
    if isinstance(scrubbed, dict) and scrubbed is not result:
        result.clear()
        result.update(scrubbed)

    def model_visible_size() -> int:
        sanitized = sanitize_tool_result_for_llm("click", result)
        return len(json.dumps(mark_mcp_result_untrusted_for_llm(sanitized)))

    def bound_enrichment_text(
        container: dict[str, Any],
        key: str,
        max_chars: int = _FAILED_CLICK_ENRICHMENT_TEXT_MAX_CHARS,
    ) -> None:
        value = container.get(key)
        if not isinstance(value, str) or len(value) <= max_chars:
            return
        prefix_chars = max_chars - len(_FAILED_CLICK_TRUNCATION_SUFFIX)
        container[key] = value[:prefix_chars] + _FAILED_CLICK_TRUNCATION_SUFFIX

    data = result.get("data")
    if not isinstance(data, dict):
        return
    attempted_control = data.get("attempted_control")
    if not isinstance(attempted_control, dict):
        return
    if model_visible_size() <= _SCOUT_RESULT_CHAR_CAP:
        return

    # Page facts are optional enrichment. Shed their detail before reducing the attempted-control
    # packet or typed failure, including the minimal shed marker left by the summary builder.
    page = data.get("page")
    if isinstance(page, dict):
        raw_shed = page.get("shed")
        shed = [value for value in raw_shed if isinstance(value, str)] if isinstance(raw_shed, list) else []
        while model_visible_size() > _SCOUT_RESULT_CHAR_CAP:
            section = _shed_scout_page_summary_section(page)
            if section is None:
                data.pop("page", None)
                break
            shed.append(section)
            page["shed"] = shed
    result.pop("warnings", None)
    for key in (
        "selector_candidates",
        "role_name_match_count",
        "selector_match_count",
        "ambiguous",
        "accessible_name",
        "role",
    ):
        if model_visible_size() <= _SCOUT_RESULT_CHAR_CAP:
            return
        attempted_control.pop(key, None)

    # The typed failure is the tool's authoritative result and must remain unchanged. Compact only
    # enriched control/location detail, retaining stable prefixes and explicit truncation markers.
    compactable_enrichment_fields = (
        (attempted_control, "selector"),
        (attempted_control, "effective_target"),
        (data, "url"),
    )
    for container, key in compactable_enrichment_fields:
        bound_enrichment_text(container, key)

    minimum_chars = len(_FAILED_CLICK_TRUNCATION_SUFFIX) + 1
    while model_visible_size() > _SCOUT_RESULT_CHAR_CAP:
        remaining = [
            (container, key, value)
            for container, key in compactable_enrichment_fields
            if isinstance((value := container.get(key)), str) and len(value) > minimum_chars
        ]
        if not remaining:
            break
        container, key, value = max(remaining, key=lambda item: len(item[2]))
        bound_enrichment_text(container, key, max(minimum_chars, len(value) // 2))


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
    if _copilot_authoring_capability(ctx).agent_blocks and normalized == "task":
        return {
            "ok": True,
            "data": {
                "block_type": "task",
                "summary": "Run a general browser task with the Task V3 engine.",
                "schema": _task_v3_pure_schema(TaskBlockYAML.model_json_schema()),
                "agent_block_guidance": _agent_block_schema_guidance(),
            },
        }
    policy_entry = _copilot_block_policy(normalized, ctx)
    if policy_entry is None:
        return None
    normalized, policy = policy_entry
    _record_code_native_pending_capability(ctx, policy)
    return {
        "ok": False,
        "error": (
            f"Block type {block_type!r} is not available in the workflow copilot. "
            f"{_render_block_policy_detail(normalized, policy)} {_copilot_banned_block_alternatives(ctx)}"
        ),
    }


_BLOCK_JSON_ALIASES = ("block", "block_definition", "definition", "block_yaml")


def _normalize_block_json_alias(params: dict[str, Any]) -> None:
    """Promote a misnamed block payload (e.g. ``block``) to ``block_json`` in place.

    The model sometimes passes the block under a shorter key than the schema's
    ``block_json``; without this, FastMCP rejects the whole call at signature
    validation before the tool runs. Stray alias keys are always dropped so they
    cannot trip the "unexpected keyword argument" check.
    """
    has_block_json = isinstance(params.get("block_json"), str) and bool(params["block_json"].strip())
    promoted: str | None = None
    for alias in _BLOCK_JSON_ALIASES:
        if alias not in params:
            continue
        value = params.pop(alias)
        if has_block_json or promoted is not None:
            continue
        if isinstance(value, str):
            promoted = value
        elif isinstance(value, (dict, list)):
            promoted = json.dumps(value)
    if promoted is not None:
        params["block_json"] = promoted


async def _validate_block_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    _normalize_block_json_alias(params)
    block_json = params.get("block_json")
    if not isinstance(block_json, str):
        return None
    try:
        raw = json.loads(block_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    violations = _block_authoring_violations(
        raw, _copilot_authoring_capability(ctx), run_names=workflow_run_names(ctx.workflow_yaml)
    )
    if not violations:
        return None
    for violation in violations:
        policy_entry = _copilot_block_policy(violation.block_type, ctx)
        if policy_entry is not None:
            _record_code_native_pending_capability(ctx, policy_entry[1])
    return {
        "ok": False,
        "error": _authoring_violation_reject_message(violations, ctx),
        "data": {"violations": [violation.as_dict() for violation in violations]},
    }


async def _get_block_schema_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    """Scrub banned block types from list-mode responses. Belt-and-suspenders
    against future drift in ``BLOCK_SUMMARIES`` (which currently omits them)."""
    capability = _copilot_authoring_capability(ctx)
    data = result.get("data")
    if isinstance(data, dict):
        block_types = data.get("block_types")
        if isinstance(block_types, dict):
            for banned in _copilot_banned_block_types(ctx):
                block_types.pop(banned, None)
            if capability.agent_blocks:
                for agent_block_type, summary in AGENT_FAMILY_BLOCK_SUMMARIES.items():
                    block_types[agent_block_type] = summary
            if capability.code_blocks and "code" in block_types:
                block_types["code"] = CODE_BLOCK_SUMMARY
            if capability.code_blocks and capability.agent_blocks:
                data["choosing_a_block_family"] = AUTHORING_FAMILY_GUIDANCE
            data["count"] = len(block_types)
        block_type = data.get("block_type")
        if (
            capability.agent_blocks
            and isinstance(block_type, str)
            and block_type in _AGENT_FAMILY_BLOCK_TYPES
            and isinstance(data.get("schema"), dict)
        ):
            data["schema"] = _task_v3_pure_schema(data["schema"])
            data["agent_block_guidance"] = _agent_block_schema_guidance()
        if capability.code_blocks and block_type == "code":
            schema = data.get("schema")
            if isinstance(schema, dict):
                properties = schema.get("properties")
                if isinstance(properties, dict) and isinstance(properties.get("prompt"), dict):
                    properties["prompt"] = {
                        "type": "string",
                        "description": (
                            "Every new or wholly rewritten code block must include this non-null string: "
                            "the model-authored plain-language Goal shown in the editor, written for this code. "
                            "Open with one sentence naming only what a person sees on the page once the block has "
                            "succeeded, showing every value the block returns as it appears there, with no actions, "
                            "conditions, 'after' or 'returns' clauses. Then give the route that reaches "
                            "it: the site or page the block works on, the controls it uses by their visible label "
                            "or role, the order it acts in, the inputs it reads by workflow parameter name (never "
                            "a value), and what the block returns, naming only page content that first sentence "
                            "already shows, never a data structure. A few sentences, enough for an agent to redo "
                            "this block on a live browser without reading the code. Rewrite the Goal whenever you "
                            "rewrite the code."
                        ),
                    }
                    properties["data_schema"] = {
                        "type": ["object", "null"],
                        "description": (
                            "JSON schema of what this block's return produces, authored for every new or wholly "
                            "rewritten code block: an object schema whose property keys are exactly the keys of "
                            "the returned dict, or, when the return is a top-level list, an array schema whose "
                            "`items` is that object schema; null only when the block returns nothing. Mark a key "
                            "`required` only when the block cannot have done its job without it. Rewrite the "
                            "schema whenever you rewrite the return."
                        ),
                    }
                    required = schema.get("required")
                    required_fields = required if isinstance(required, list) else []
                    schema["required"] = [
                        *required_fields,
                        *(field for field in ("prompt", "data_schema") if field not in required_fields),
                    ]
            if not capability.agent_blocks:
                data["code_only_note"] = _code_only_browser_unavailable_summary()
            data["code_only_guidance"] = _code_only_browser_schema_guidance(agent_blocks=capability.agent_blocks)
            data["download_claim_helper_contract"] = download_claim_helper_contract()
            data["web_search_helper_contract"] = WEB_SEARCH_HELPER_CONTRACT
            data["clear_browser_data_helper_contract"] = CLEAR_BROWSER_DATA_HELPER_CONTRACT
            data["open_page_helper_contract"] = OPEN_PAGE_HELPER_CONTRACT
            data["dialog_policy_helper_contract"] = DIALOG_POLICY_HELPER_CONTRACT
            page_operation_contracts = app.AGENT_FUNCTION.page_operation_contracts()
            if page_operation_contracts is not None:
                data["page_operation_contracts"] = page_operation_contracts
            execution_limits = await app.AGENT_FUNCTION.codeblock_execution_limits(
                organization_id=ctx.organization_id,
                workflow_permanent_id=ctx.workflow_permanent_id,
            )
            if execution_limits is not None:
                data["code_execution_limits"] = execution_limits
            demonstrated = _demonstrated_step_facts(ctx)
            if demonstrated:
                data["demonstrated_steps"] = demonstrated
    return result


def _task_v3_pure_schema(schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
    policy_schema = copy.deepcopy(schema)
    properties = policy_schema.get("properties")
    if isinstance(properties, dict):
        properties["engine"] = {
            "type": "string",
            "description": _ENGINE_FIELD_DESCRIPTION,
        }
        required = policy_schema.get("required")
        if isinstance(required, list) and "engine" in required:
            required.remove("engine")
    return policy_schema


_ENGINE_FIELD_DESCRIPTION = (
    f"Omit on a new block: Copilot-authored agent blocks run on the {_TASK_V3_ENGINE} engine and it is filled in. "
    "A saved block keeps the engine it has unless the user asks to change it."
)


def _agent_block_schema_guidance() -> list[str]:
    return [
        _ENGINE_FIELD_DESCRIPTION,
        "Use engine-less blocks for orchestration, integrations, direct navigation, waits, files, and human interaction.",
        "Use `loop_over_parameter_key` for for_loop input and explicit `jinja2_template` criteria for conditional or while_loop control flow.",
        "task_v2, free-form for_loop inputs, prompt control-flow criteria, and download-gated validation are unavailable.",
    ]


async def _get_workflow_knowledge_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    capability = _copilot_authoring_capability(ctx)
    data = result.get("data")
    if isinstance(data, dict) and capability.code_blocks and capability.agent_blocks:
        sections = data.get("sections")
        if isinstance(sections, dict):
            choosing = sections.get("choosing_a_block")
            if isinstance(choosing, dict) and isinstance(choosing.get("content"), str):
                choosing["content"] = f"{AUTHORING_FAMILY_GUIDANCE}\n\n{choosing['content']}"
    return result


_MODEL_SCOUT_FACT_KEYS = (
    "tool_name",
    "executed_selector",
    "selector_candidates",
    "role",
    "accessible_name",
    "role_name_match_count",
    "source_url",
    "result_url",
    "observed_effects",
    "challenge_vendor",
    "observed_wait_ms",
    "observation_step",
    "input_id",
    "value",
    "typed_length",
    "key",
    "control_readonly",
    "control_disabled",
    "control_value_satisfied",
    "observed_hidden",
    "observed_disabled",
    "ambiguous",
    "input_correspondences",
    "credential_id",
    "credential_name",
    "credential_field",
    "element_fingerprint_id",
    "element_fingerprint_name",
    "element_fingerprint_type",
    "element_fingerprint_placeholder",
    "element_fingerprint_label",
    "element_fingerprint_test_id",
    "element_fingerprint_tag",
    "read_expression",
    "read_output_path",
    "read_output_path_source",
    "read_result_shape",
    "read_result_value",
    "trajectory_index",
    # Marks an entry hydrated from a prior turn's record. Without it the model reads retained
    # history as something it just did on the page in front of it.
    "carried",
)

_MODEL_SCOUT_NULLABLE_FACT_KEYS = (
    "selector_candidates",
    "role",
    "accessible_name",
    "role_name_match_count",
    "source_url",
    "result_url",
    "observed_effects",
    "observation_step",
    "input_id",
)


def _demonstrated_step_facts(ctx: AgentContext) -> list[dict[str, Any]]:
    """Ordered, factual scout input for the acting model; never synthesized browser source."""
    if not ctx.scout_trajectory:
        return []
    facts = []
    for interaction in ctx.scout_trajectory:
        interaction_mapping: Mapping[str, Any] = interaction
        fact = {key: interaction_mapping[key] for key in _MODEL_SCOUT_FACT_KEYS if key in interaction_mapping}
        internal_selector = interaction_mapping.get("selector")
        if "executed_selector" not in fact and isinstance(internal_selector, str) and internal_selector:
            fact["executed_selector"] = internal_selector
        raw_candidates = fact.get("selector_candidates")
        if isinstance(raw_candidates, list):
            fact["selector_candidates"] = [
                {key: value for key, value in candidate.items() if key != "match_count"}
                if isinstance(candidate, dict) and isinstance(candidate.get("selector"), str)
                else candidate
                for candidate in raw_candidates
            ]
        for key in ("source_url", "result_url"):
            value = fact.get(key)
            if isinstance(value, str):
                fact[key] = safe_page_origin(value)
        for key in _MODEL_SCOUT_NULLABLE_FACT_KEYS:
            fact.setdefault(key, None)
        facts.append(fact)
    secrets = registered_scrub_values(ctx)

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "[REDACTED_SECRET]")
            return redact_raw_secrets_for_prompt(value)
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        return value

    return [scrub(fact) for fact in facts]


async def _evaluate_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    # Cleared up front so an early reject cannot leave a prior evaluate's expression for this
    # call's post-hook to consume.
    ctx.pending_scout_read_expression = None
    ctx.pending_scout_read_output_path = None
    sensitive_page_refusal = _sensitive_origin_page_action_refusal(ctx)
    if sensitive_page_refusal is not None:
        return sensitive_page_refusal
    raw_expression = params.get("expression")
    if isinstance(raw_expression, str) and raw_expression.strip():
        ctx.pending_scout_read_expression = raw_expression
        raw_output_path = params.get("output_path")
        if isinstance(raw_output_path, str) and raw_output_path.strip():
            ctx.pending_scout_read_output_path = raw_output_path.strip()
    return None


async def _screenshot_pre_hook(_params: dict[str, Any], ctx: AgentContext) -> dict[str, Any] | None:
    if ctx.codeblock_redaction_parameters:
        return {"ok": False, "error": "Screenshots are unavailable during the code block AI fallback."}
    return _sensitive_origin_page_refusal(ctx)


async def _scroll_pre_hook(params: dict[str, Any], ctx: AgentContext) -> dict[str, Any] | None:
    intent = params.get("intent")
    if ctx.codeblock_redaction_parameters and isinstance(intent, str) and intent.strip():
        return {"ok": False, "error": "AI-assisted scrolling is unavailable during the code block AI fallback."}
    return _sensitive_origin_page_action_refusal(ctx)


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


# Bounds the frames only visible_effect needs, well under the screenshot tool's own action deadline so
# a slow capture is cancelled here rather than recorded as a browser-health strike.
_VISIBLE_EFFECT_FRAME_TIMEOUT_SECONDS = 2.0


async def _click_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    # Cleared up front so an early return below (deterministic result or no selector)
    # cannot leave a prior click's stash for this click's post-hook to consume.
    ctx.pending_scout_click_selector = None
    ctx.pending_scout_download_snapshot = None
    ctx.pending_scout_download = False
    ctx.pending_scout_popup = None
    ctx.pending_scout_popup_content_type = None
    ctx.pending_scout_challenge_frames = []
    ctx.pending_scout_challenge_prior_frames = []
    ctx.pending_scout_challenge_armed_at = None
    ctx.pending_scout_click_records = []
    ctx.pending_scout_click_pre_frame = None
    _release_scout_challenge_listeners(ctx)
    sensitive_page_refusal = _sensitive_origin_page_action_refusal(ctx)
    if sensitive_page_refusal is not None:
        return sensitive_page_refusal
    await _capture_scout_source_url(ctx)
    selector = params.get("selector", "")
    await _capture_scout_pre_action(ctx, selector if isinstance(selector, str) else None)
    ctx.pending_scout_click_pre_frame = await _take_viewport_frame(
        ctx, timeout_seconds=_VISIBLE_EFFECT_FRAME_TIMEOUT_SECONDS
    )
    # Armed before the selector check: an intent or coordinate click carries no selector going in but
    # reports the element it resolved, and the challenge it raises is recorded against that.
    await _arm_scout_challenge_listener(ctx)
    if selector:
        ctx.pending_scout_click_selector = selector if isinstance(selector, str) else None
        if _copilot_authoring_capability(ctx).code_blocks:
            ctx.pending_scout_download_snapshot = await _scout_session_download_names(ctx)
            await _arm_scout_download_listener(ctx)
            await _arm_scout_popup_listener(ctx)
    await _close_scout_challenge_baseline(ctx)
    return None


async def _type_text_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    _clear_pending_scout_selector_facts(ctx)
    ctx.pending_scout_input_value = None
    sensitive_page_refusal = _sensitive_origin_page_action_refusal(ctx)
    if sensitive_page_refusal is not None:
        return sensitive_page_refusal
    await _capture_scout_source_url(ctx)
    text = params.get("text")
    selector = str(params.get("selector") or "")
    # A value already registered as a credential is known to be secret, so it is rejected on that
    # fact rather than on whether it looks secret — a real password need not, and this one reached a
    # plaintext username field because it did not.
    typed_is_registered_secret = isinstance(text, str) and bool(text) and text in set(registered_scrub_values(ctx))
    if typed_is_registered_secret:
        return {
            "ok": False,
            "error": (
                "type_text cannot type an exact value already registered as a secret. Use "
                "fill_credential_field with the saved credential and the field it belongs in, rather "
                "than typing that registered value into a field yourself."
            ),
        }
    if isinstance(text, str) and text:
        ctx.pending_scout_input_value = text
    await _capture_scout_pre_action(ctx, selector)
    return None


async def _select_option_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    sensitive_page_refusal = _sensitive_origin_page_action_refusal(ctx)
    if sensitive_page_refusal is not None:
        return sensitive_page_refusal
    await _capture_scout_source_url(ctx)
    await _capture_scout_pre_action(ctx, params.get("selector", ""))
    return None


async def _press_key_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    sensitive_page_refusal = _sensitive_origin_page_action_refusal(ctx)
    if sensitive_page_refusal is not None:
        return sensitive_page_refusal
    await _capture_scout_source_url(ctx)
    await _capture_scout_pre_action(ctx, params.get("selector"))
    return None


async def _bind_login_credential_for_observed_url(ctx: AgentContext, url: str, result: dict[str, Any]) -> None:
    """Surface the saved credential the observed page vouches for, if the server can bind one.

    A resolver or loader failure leaves the page observation exactly as it was, but the context
    reads stay outside that guard: swallowing them would turn a mis-wired context into a silent
    no-op that no test can catch.
    """
    if not url:
        return

    policy = ctx.request_policy
    if not isinstance(policy, RequestPolicy) or not live_page_credentials_admissible(policy):
        return
    organization_id = ctx.organization_id

    if not is_resolved_page_url(url):
        # A `current_page` inspection stamps its placeholder when the URL read raced the capture.
        # The match is against the page the browser actually reached, so read it again rather than
        # hand over a token no page vouches for.
        url, _ = await _fallback_page_info(ctx)

    async def load_once() -> list[Credential]:
        if ctx.org_credentials_for_turn is None:
            ctx.org_credentials_for_turn = await load_credentials(organization_id)
        return ctx.org_credentials_for_turn

    try:
        record = await resolve_credential_for_live_page(
            policy,
            organization_id=organization_id,
            page_url=url,
            load_org_credentials=load_once,
        )
    except Exception:
        LOG.warning(
            "copilot credential live-page admission",
            outcome="resolver_error",
            seam="page_observation",
        )
        return

    if record.verdict == "resolved" and record.candidates:
        await record_signed_out_page_observation(ctx, url)
        credential = record.candidates[0]
        result["resolved_login_credential_id"] = credential.credential_id
        result["resolved_login_credential_name"] = credential.name
        result["resolved_login_credential_totp_type"] = str(credential.totp_type)
        # The observation may name a placeholder while the match ran against the page read after it,
        # so the page the credential was matched on is reported rather than left to be inferred.
        result["resolved_login_page_url"] = url
    elif record.verdict == "ambiguous":
        result["candidate_login_credentials"] = [
            {"credential_id": candidate.credential_id, "name": candidate.name} for candidate in record.candidates
        ]


async def _navigate_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    _start_scout_challenge_settle(ctx)
    try:
        return await _navigate_post_hook_body(result, raw, ctx)
    finally:
        ctx.pending_scout_challenge_frames = []
        ctx.pending_scout_challenge_prior_frames = []
        ctx.pending_scout_challenge_armed_at = None
        _release_scout_challenge_listeners(ctx)


async def _navigate_post_hook_body(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    _clear_pending_browser_interaction_observation(ctx)
    sensitive_origin_page_was_tainted = sensitive_origin_page_is_tainted(ctx)
    captured_source_url = _consume_scout_source_url(ctx)
    source_url = None if sensitive_origin_page_was_tainted else captured_source_url
    if result.get("ok"):
        data = result.pop("data", {})
        result["url"] = data.get("url", "")
        # Raw against raw: `result["url"]` is already secret-scrubbed, so against the raw before-URL a
        # fragment hop on a page whose URL holds a registered value would differ only by the redaction.
        if sensitive_origin_page_was_tainted:
            # The staged source outlives a hold: the sensitive document stays what the next navigation
            # is judged against, or re-navigating to this page after closing the tabs never lifts.
            taint_source_url = pending_taint_source_url(ctx)
            live_url = await live_working_page_url(ctx)
            if not await clear_sensitive_origin_page_taint_after_navigation(
                ctx, source_url=taint_source_url, result_url=live_url
            ):
                # Still withheld, so not even the URL goes back; the code tool and the navigating
                # inspection refuse the same way. Which hold it is decides what the model can do next.
                if navigation_replaced_document(taint_source_url, live_url):
                    return {"ok": False, "error": await sensitive_origin_multi_tab_error(ctx)}
                return {"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR}
        _record_scouted_interaction(
            ctx,
            tool_name="navigate_browser",
            source_url=source_url,
            result_url=result["url"],
        )
        await _bind_login_credential_for_observed_url(ctx, result["url"], result)
        staged = await _capture_post_interaction_screenshot(
            ctx,
            source_tool="navigate_browser",
            captured_url=result["url"],
        )
        await attach_navigation_challenge_vendor(ctx, result)
        attached = " A screenshot is attached." if staged else ""
        result["next_step"] = (
            f"Page loaded.{attached} Use evaluate or inspect_page_for_composition when you need the "
            "page's structure or selectors before responding."
        )
    elif not sensitive_origin_page_was_tainted:
        await _capture_post_interaction_screenshot(
            ctx,
            source_tool="navigate_browser",
            captured_url=source_url,
        )
    return await attribute_navigation_failure(ctx, result)


async def _navigate_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    if sensitive_origin_page_has_active_run(ctx):
        ctx.pending_scout_source_url = None
        return {"ok": False, "error": SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR}
    if sensitive_origin_page_is_tainted(ctx):
        ctx.pending_scout_source_url = None
        await stage_pending_taint_source(ctx)
        return None
    await _capture_scout_source_url(ctx)
    await _arm_scout_challenge_listener(ctx)
    return None


async def _wait_for_either_state_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    sensitive_page_refusal = _sensitive_origin_page_action_refusal(ctx)
    if sensitive_page_refusal is not None:
        return sensitive_page_refusal
    data = result.get("data")
    if not isinstance(data, dict):
        return result
    selector_a = data.get("selector_a")
    selector_b = data.get("selector_b")
    candidates: list[ScoutedSelectorCandidate] = [
        {"selector": selector, "source": source, "match_count": None}
        for selector, source in ((selector_a, "selector_a"), (selector_b, "selector_b"))
        if isinstance(selector, str) and selector
    ]
    _record_scouted_interaction(
        ctx,
        tool_name="wait_for_either_state",
        selector=data.get("matched_selector"),
        selector_candidates=candidates or None,
        source_url=data.get("source_url"),
        result_url=data.get("result_url"),
        observed_wait_ms=data.get("observed_wait_ms") if isinstance(data.get("observed_wait_ms"), int) else None,
    )
    return result


async def _screenshot_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    sensitive_page_refusal = _sensitive_origin_page_refusal(ctx)
    if sensitive_page_refusal is not None:
        return sensitive_page_refusal
    if result.get("ok") and result.get("data"):
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
    _start_scout_challenge_settle(ctx)
    try:
        return await _click_post_hook_body(result, raw, ctx)
    finally:
        ctx.pending_scout_challenge_frames = []
        ctx.pending_scout_challenge_armed_at = None
        _release_scout_challenge_listeners(ctx)


async def _click_post_hook_body(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    pre_frame = ctx.pending_scout_click_pre_frame
    ctx.pending_scout_click_pre_frame = None
    ctx.last_scout_act_observe_outcome = None
    ctx.last_scout_act_observe_packet = None
    page_evidence: dict[str, Any] | None = None
    captured_url: str | None = None
    observation_step: int | None = None
    failed_with_selector = False
    success_summary_evidence: dict[str, Any] | None = None
    _clear_pending_browser_interaction_observation(ctx)
    sensitive_page_refusal = _sensitive_origin_page_action_refusal(ctx)
    if sensitive_page_refusal is not None:
        return sensitive_page_refusal
    source_url = _consume_scout_source_url(ctx)
    pending_role_name = ctx.pending_scout_role_name
    ctx.pending_scout_role_name = None
    pending_role_name_match_count = getattr(ctx, "pending_scout_role_name_match_count", None)
    ctx.pending_scout_role_name_match_count = None
    pending_ambiguous = ctx.pending_scout_ambiguous
    ctx.pending_scout_ambiguous = None
    pending_selector_match_count = getattr(ctx, "pending_scout_selector_match_count", None)
    ctx.pending_scout_selector_match_count = None
    pending_selector_candidates = getattr(ctx, "pending_scout_selector_candidates", None)
    ctx.pending_scout_selector_candidates = None
    ctx.pending_scout_reanchor = None
    pending_click_selector = ctx.pending_scout_click_selector
    ctx.pending_scout_click_selector = None
    if result.get("ok") and result.get("data"):
        data = result["data"]
        selector_candidates = _merge_selector_candidates(
            _selector_candidates_from_tool_data(data), pending_selector_candidates
        )
        selector = _selector_from_tool_data(data, prefer_resolved_when_empty=True)
        url, title = await _resolve_url_title(raw, ctx)
        _mark_pending_browser_interaction_observation(ctx, tool_name="click", url=url)
        result["data"] = {
            "executed_selector": selector,
            "url": safe_page_origin(url) or "",
            "title": title,
        }
        await _bind_login_credential_for_observed_url(ctx, url, result)
        navigated = bool(source_url) and bool(url) and source_url != url
        role, accessible_name = _prenav_role_name_for_selector(pending_role_name, selector)
        if not (role and accessible_name):
            role, accessible_name = await _resolve_scout_role_name(ctx, selector, allow_browser_read=not navigated)
        ambiguous = _prenav_ambiguity_for_selector(pending_ambiguous, selector)
        selector_match_count = (
            pending_selector_match_count[1]
            if isinstance(pending_selector_match_count, tuple)
            and len(pending_selector_match_count) == 2
            and pending_selector_match_count[0] == selector
            else None
        )
        role_name_match_count = (
            pending_role_name_match_count[3]
            if isinstance(pending_role_name_match_count, tuple)
            and len(pending_role_name_match_count) == 4
            and pending_role_name_match_count[:3] == (selector, role, accessible_name)
            else None
        )
        result["data"]["effective_target"] = _effective_target_text(selector, role, accessible_name)
        _record_scouted_interaction(
            ctx,
            tool_name="click",
            selector=selector,
            selector_candidates=selector_candidates,
            selector_match_count=selector_match_count,
            source_url=source_url,
            result_url=url,
            role=role,
            accessible_name=accessible_name,
            role_name_match_count=role_name_match_count,
            ambiguous=ambiguous,
        )
        observation_step, page_evidence = await _register_scout_interaction_observation(
            ctx, tool_name="click", selector=selector, source_url=source_url, url=url
        )
        _attach_scout_observation_step(
            ctx,
            tool_name="click",
            selector=selector,
            observation_step=observation_step,
        )
        if observation_step is not None:
            result["observation_step"] = observation_step
            result["data"]["observation_step"] = observation_step
        captured_url = url or None
        if _copilot_authoring_capability(ctx).code_blocks:
            # A download this click produced is proof the affordance works, so it outranks the
            # href-shape prediction — and is the only source that sees a command-URL download.
            await _maybe_attach_observed_download_target(ctx, result, selector=selector, url=url)
            await _maybe_attach_observed_render_target(ctx, result, selector=selector, url=url)
        await _maybe_attach_observed_challenge(ctx, result, url=url)
        if page_evidence is not None:
            success_summary_evidence = page_evidence
        elif ctx.last_scout_act_observe_outcome == "unchanged":
            result["data"]["page_observation"] = {
                "status": "unchanged",
                "message": (
                    "The page observation did not change after the click; no post-click page evidence was attached."
                ),
            }
    elif not result.get("ok") and isinstance(pending_click_selector, str) and pending_click_selector.strip():
        selector = pending_click_selector.strip()
        role, accessible_name = _prenav_role_name_for_selector(pending_role_name, selector)
        selector_match_count = (
            pending_selector_match_count[1]
            if isinstance(pending_selector_match_count, tuple)
            and len(pending_selector_match_count) == 2
            and pending_selector_match_count[0] == selector
            else None
        )
        role_name_match_count = (
            pending_role_name_match_count[3]
            if isinstance(pending_role_name_match_count, tuple)
            and len(pending_role_name_match_count) == 4
            and pending_role_name_match_count[:3] == (selector, role, accessible_name)
            else None
        )
        attempted_control = _failed_click_attempted_control(
            selector=selector,
            selector_candidates=pending_selector_candidates,
            selector_match_count=selector_match_count,
            role=role,
            accessible_name=accessible_name,
            role_name_match_count=role_name_match_count,
            ambiguous=_prenav_ambiguity_for_selector(pending_ambiguous, selector),
        )
        url, _ = await _resolve_url_title(raw, ctx)
        result["data"] = {
            "attempted_control": attempted_control,
            "url": safe_page_origin(url) or "",
        }
        location_fingerprint = _page_evidence_location_fingerprint(url)
        if location_fingerprint is not None:
            result["data"]["current_url_location_fingerprint"] = location_fingerprint
        captured_url = url or None
        if url:
            page_evidence = await _scout_act_observe_page_evidence(ctx, url=url)
            if page_evidence is not None:
                _attach_scout_page_summary(ctx, result, page_evidence)
        # A handler can mount a challenge and then stall the navigation the click was waiting on.
        await _maybe_attach_observed_challenge(ctx, result, url=url)
        failed_with_selector = True
    else:
        await _maybe_attach_observed_challenge(ctx, result, url=source_url or "")
    stage_frame = ctx.last_scout_act_observe_outcome != "attached" or not _page_evidence_names_obstruction(
        page_evidence
    )
    post_frame = None
    if pre_frame is not None:
        post_frame = await _take_viewport_frame(
            ctx,
            timeout_seconds=(
                _DISCOVERY_PER_CALL_TIMEOUT_SECONDS
                if stage_frame and ctx.supports_vision
                else _VISIBLE_EFFECT_FRAME_TIMEOUT_SECONDS
            ),
        )
    if not isinstance(result.get("data"), dict):
        result["data"] = {}
    result["data"]["visible_effect"] = viewport_visible_effect(pre_frame, post_frame)
    # Both size bounds shed against the whole result, so they run after the last key is written.
    if success_summary_evidence is not None:
        _attach_scout_page_summary(ctx, result, success_summary_evidence)
    if failed_with_selector:
        _bound_failed_click_result(ctx, result)
    # No frame is staged when the post frame just failed, since a retry would wait out the same stall, or
    # when the evidence positively names the obstruction a frame would show (merely parsed evidence is not).
    if stage_frame and not (pre_frame is not None and post_frame is None):
        await _capture_post_interaction_screenshot(
            ctx,
            source_tool="click",
            captured_url=captured_url or source_url,
            observation_step=observation_step,
            frame=post_frame,
        )
    return result


async def _safe_composition_evidence(ctx: AgentContext, url: str, *, timeout_seconds: float) -> dict[str, Any] | None:
    if timeout_seconds <= 0:
        return None
    try:
        return await _composition_get_structured_evidence(
            ctx,
            inspected_url=url,
            current_url=url,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        return None


_TYPE_READBACK_SETTLE_SECONDS = 0.3


async def _read_scout_field_value(ctx: AgentContext, selector: str) -> str | None:
    """Read a field's current value through the discovery MCP surface, or None when unavailable."""
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return None
    try:
        readback = await asyncio.wait_for(
            server.call_internal_tool("skyvern_get_value", {"selector": selector}),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except Exception:
        LOG.debug("scout field-value read failed; leaving the value unread")
        return None
    if not isinstance(readback, dict) or not readback.get("ok"):
        return None
    value = (readback.get("data") or {}).get("value")
    return value if isinstance(value, str) else None


_XPATH_SELECTOR_RE = re.compile(r"^\s*(?:xpath=|\(?/)")
_ENGINE_PREFIXED_SELECTOR_RE = re.compile(r"^\s*[A-Za-z][\w-]*=")


def _selector_supports_control_state_probe(selector: str) -> bool:
    """Only bare CSS and XPath resolve inside the probe expression. A Playwright-engine selector
    (``role=``, ``text=``, or a ``>>`` chain) would throw in-page and cost a round-trip to learn nothing.
    """
    if _XPATH_SELECTOR_RE.match(selector):
        return True
    return ">>" not in selector and not _ENGINE_PREFIXED_SELECTOR_RE.match(selector)


async def _probe_scout_control_state(ctx: AgentContext, selector: str) -> tuple[bool | None, bool | None]:
    """Return (readonly, disabled) booleans for a captured type_text target, either None when the control
    state cannot be resolved (unavailable surface, unresolvable/non-CSS-or-XPath selector). No raw field
    value crosses this boundary — the evaluate reads attributes only.
    """
    if not isinstance(selector, str) or not selector.strip():
        return None, None
    if not _selector_supports_control_state_probe(selector):
        return None, None
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return None, None
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool(
                "skyvern_evaluate",
                {"expression": scout_control_state_expression(selector), "verbosity": "full"},
            ),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except Exception:
        LOG.debug("scout control-state probe failed; treating editability as unknown")
        return None, None
    if not isinstance(result, dict) or not result.get("ok"):
        return None, None
    state = (result.get("data") or {}).get("result")
    if not isinstance(state, dict):
        return None, None
    return bool(state.get("readonly")), bool(state.get("disabled"))


class ScoutReadbackOutcome(StrEnum):
    """Factual readback outcome: what was observed in the field, not a verdict about where it belongs."""

    EXACT_MATCH = "exact_match"
    EMPTY = "empty"
    DIFFERENT = "different"
    UNAVAILABLE = "unavailable"


def _scout_readback_outcome(readback: str | None, typed_value: str) -> ScoutReadbackOutcome:
    """Report the factual readback outcome for a fill, sound only for a readback taken from the page
    itself: a value read back through the tool layer has any registered secret replaced by a
    placeholder, so its content describes the scrubber and not the field (`GOTCHAS.md` §28).
    """
    if not isinstance(readback, str):
        return ScoutReadbackOutcome.UNAVAILABLE
    # Equality is tested first so a typed value that is itself blank and lands exactly reads as a
    # match rather than as the empty field the fill lost.
    if readback == typed_value:
        return ScoutReadbackOutcome.EXACT_MATCH
    if readback.strip() == "":
        return ScoutReadbackOutcome.EMPTY
    return ScoutReadbackOutcome.DIFFERENT


def _scout_type_landing_failure(
    outcome: ScoutReadbackOutcome,
    *,
    tool_name: str,
    selector: str,
) -> dict[str, Any] | None:
    if outcome is ScoutReadbackOutcome.EMPTY:
        return {
            "ok": False,
            "error": (
                f"{tool_name} reported success but the field is still empty. "
                f"Re-inspect the current page and retry {tool_name} on the target field. "
                "If an overlay (cookie/marketing popup) consumed the focus, the first "
                "interaction usually dismisses it."
            ),
        }
    return None


async def _verify_scout_type_landed(
    ctx: AgentContext,
    *,
    selector: str,
    typed_length: Any,
    prefetched_value: str | None = None,
) -> dict[str, Any] | None:
    """Confirm a non-empty type actually entered the field, else return a failure.

    A marketing/cookie overlay can consume the focus or keystrokes — the field
    stays empty while `skyvern_type` still reports success (the first interaction
    on an overlaid page often just dismisses the overlay). Read the field back; a
    field still empty after a non-empty type means the input did not land. Only
    fires when there is a selector to read and a positive typed length, so it never
    second-guesses intent-only types or masked/formatted values, which keep a
    non-empty value.

    The readback rides the tool layer, which replaces any registered secret it finds — including
    one the field already held, not only one this call typed. A caller that fills a registered
    secret must classify its own readback and call `_scout_type_landing_failure` instead; a
    readback of a field still holding a credential filled earlier in the same browser session is
    inflated the same way and is not covered here.
    """
    if not isinstance(selector, str) or not selector.strip():
        return None
    if not isinstance(typed_length, int) or typed_length <= 0:
        return None
    if getattr(ctx, "discovery_mcp_server", None) is None:
        return None

    value = prefetched_value if prefetched_value is not None else await _read_scout_field_value(ctx, selector)
    if isinstance(value, str) and value.strip() == "":
        # A controlled/React input can mirror its value asynchronously, so a first read may be
        # transiently empty; settle briefly and re-read once before declaring the type lost.
        await asyncio.sleep(_TYPE_READBACK_SETTLE_SECONDS)
        value = await _read_scout_field_value(ctx, selector)
    if isinstance(value, str) and value.strip() == "":
        return _scout_type_landing_failure(
            ScoutReadbackOutcome.EMPTY,
            tool_name="type_text",
            selector=selector,
        )
    return None


async def _type_text_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    _clear_pending_browser_interaction_observation(ctx)
    sensitive_page_refusal = _sensitive_origin_page_action_refusal(ctx)
    if sensitive_page_refusal is not None:
        ctx.pending_scout_input_value = None
        return sensitive_page_refusal
    source_url = _consume_scout_source_url(ctx)
    pending_role_name = getattr(ctx, "pending_scout_role_name", None)
    ctx.pending_scout_role_name = None
    pending_role_name_match_count = getattr(ctx, "pending_scout_role_name_match_count", None)
    ctx.pending_scout_role_name_match_count = None
    pending_selector_match_count = getattr(ctx, "pending_scout_selector_match_count", None)
    ctx.pending_scout_selector_match_count = None
    pending_selector_candidates = getattr(ctx, "pending_scout_selector_candidates", None)
    ctx.pending_scout_selector_candidates = None
    pending_input_value = ctx.pending_scout_input_value
    ctx.pending_scout_input_value = None
    if result.get("ok") and result.get("data"):
        data = result["data"]
        selector_candidates = _merge_selector_candidates(
            _selector_candidates_from_tool_data(data), pending_selector_candidates
        )
        selector = _selector_from_tool_data(data)
        typed_length = data.get("text_length", 0)
        url, _ = await _resolve_url_title(raw, ctx)
        result["data"] = {
            "executed_selector": selector,
            "typed_length": typed_length,
            "url": url,
        }
        has_landing_probe = (
            isinstance(selector, str) and bool(selector.strip()) and isinstance(typed_length, int) and typed_length > 0
        )
        field_value = await _read_scout_field_value(ctx, selector) if has_landing_probe else None
        control_readonly, control_disabled = (
            await _probe_scout_control_state(ctx, selector) if has_landing_probe else (None, None)
        )
        is_readonly_or_disabled = bool(control_readonly) or bool(control_disabled)
        landing_failure = await _verify_scout_type_landed(
            ctx, selector=selector, typed_length=typed_length, prefetched_value=field_value
        )
        if landing_failure is not None and not is_readonly_or_disabled:
            return landing_failure
        _mark_pending_browser_interaction_observation(ctx, tool_name="type_text", url=url)
        role, accessible_name = await _resolve_scout_role_name(ctx, selector)
        if not (role and accessible_name):
            role, accessible_name = _prenav_role_name_for_selector(pending_role_name, selector)
        selector_match_count = (
            pending_selector_match_count[1]
            if isinstance(pending_selector_match_count, tuple)
            and len(pending_selector_match_count) == 2
            and pending_selector_match_count[0] == selector
            else None
        )
        role_name_match_count = (
            pending_role_name_match_count[3]
            if isinstance(pending_role_name_match_count, tuple)
            and len(pending_role_name_match_count) == 4
            and pending_role_name_match_count[:3] == (selector, role, accessible_name)
            else None
        )
        value_landed = (
            isinstance(typed_length, int)
            and typed_length > 0
            and isinstance(pending_input_value, str)
            and len(pending_input_value) == typed_length
        )
        input_id = f"input_{uuid.uuid4().hex}" if value_landed else ""
        input_value = pending_input_value if value_landed and isinstance(pending_input_value, str) else ""
        if is_readonly_or_disabled and isinstance(pending_input_value, str):
            settled_value = field_value
            if settled_value is not None and settled_value != pending_input_value:
                await asyncio.sleep(_TYPE_READBACK_SETTLE_SECONDS)
                settled_value = await _read_scout_field_value(ctx, selector)
            control_value_satisfied: bool | None = (
                settled_value == pending_input_value if settled_value is not None else None
            )
        else:
            control_value_satisfied = None
        _record_scouted_interaction(
            ctx,
            tool_name="type_text",
            selector=selector,
            selector_candidates=selector_candidates,
            selector_match_count=selector_match_count,
            source_url=source_url,
            result_url=url,
            typed_length=typed_length,
            input_id=input_id,
            input_value=input_value,
            role=role,
            accessible_name=accessible_name,
            role_name_match_count=role_name_match_count,
            control_readonly=control_readonly,
            control_disabled=control_disabled,
            control_value_satisfied=control_value_satisfied,
        )
        observation_step, page_evidence = await _register_scout_interaction_observation(
            ctx, tool_name="type_text", selector=selector, source_url=source_url, url=url
        )
        _attach_scout_observation_step(
            ctx,
            tool_name="type_text",
            selector=selector,
            observation_step=observation_step,
        )
        if observation_step is not None:
            result["observation_step"] = observation_step
            result["data"]["observation_step"] = observation_step
        if page_evidence is not None:
            _attach_scout_page_summary(ctx, result, page_evidence)
    return result


# composition_evidence caps a captured value_text at 240 chars, so a witness longer than that cannot
# match one and is only page text riding along in the trajectory.
_WITNESSED_READ_VALUE_MAX_CHARS = 240


def _sole_scalar_leaf(result: object) -> object | None:
    """The one scalar anywhere inside a result, or None if it holds none or more than one.

    A read that answers with a value often returns it wrapped — a one-key dict, a one-element list —
    and the wrapper says nothing about which element on the page carries it. More than one scalar
    means the read described a page instead of answering with a value, so the whole structure is
    walked; stopping early would call a shallow scalar sole while a deeper one went unseen.
    """
    found: object | None = None
    pending: list[object] = [result]
    while pending:
        current = pending.pop()
        if isinstance(current, (str, int, float)):
            if found is not None:
                return None
            found = current
        elif isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple, set)):
            pending.extend(current)
    return found


def _witnessed_scalar_value(result: object) -> str:
    """The scalar a read returned, so a later binding can find what still carries it."""
    scalar = _sole_scalar_leaf(result)
    if scalar is None or isinstance(scalar, bool):
        return ""
    text = str(scalar).strip()
    if not text or len(text) > _WITNESSED_READ_VALUE_MAX_CHARS:
        return ""
    # Redacted text cannot support the exact match this exists for, so a value redaction would alter
    # is dropped rather than kept in a form nothing can bind.
    if redact_raw_secrets_for_prompt(text) != text:
        return ""
    return text


def _record_scouted_read(
    ctx: AgentContext,
    *,
    expression: str,
    data: dict[str, Any],
    url: str,
    declared_output_path: str | None = None,
) -> ScoutedInteraction | None:
    """Keep a read the scout proved on the live page so authoring replays it instead of guessing a
    locator for a value it has already seen. A structured result is kept as readily as a scalar: the
    shape is what a later binding needs. Naming never gates keeping the evidence."""
    result = data.get("result")
    if not expression or result is None or result == "":
        return None
    if isinstance(result, (list, dict)) and not result:
        return None
    output_path = "output.scouted_read"
    witnessed = _witnessed_scalar_value(result)
    policy = ctx.request_policy
    requested: set[str] = set()
    if isinstance(policy, RequestPolicy):
        # Derivation's set, not the graded criteria projected to output_path: rekeying clears that
        # field while the criterion stays graded, so a witnessed value stayed anonymous and
        # completion verification had nothing for an outcome the scout had already seen.
        requested = requested_output_paths_for_derivation(ctx)
        named = sorted(path for path in requested if not is_canonical_request_slot_path(path))
        # A rekeyed requested output carries a digest instead of a word, which still keys the
        # producer to the criterion it has to satisfy. Reading into an anonymous path instead leaves
        # completion verification with no evidence for an outcome the scout already demonstrated.
        candidates = named or sorted(requested)
        # Owning the requested output takes a value the read actually observed, not merely being the
        # only path on offer: a read that came back holding no single value describes the page, and
        # promoting it leaves the run claiming an answer nothing witnessed.
        if len(candidates) == 1 and witnessed:
            output_path = candidates[0]
    # Counting candidates can only attribute a read when the turn requests a single output; a request
    # for several fields needs the reader to say which one it just read.
    if declared_output_path and (
        declared_output_path in requested or (not requested and declared_output_path.startswith("output."))
    ):
        output_path = declared_output_path
    # The same tool inspects a page and reads a value, so attribution by elimination promotes an
    # inspection to the requested output. Report what owning the output by declaration alone would
    # bind, so the cost of that rule is known before it decides anything.
    LOG.info(
        "copilot_scouted_read_attribution",
        declared=bool(declared_output_path),
        attributed_by="declaration" if declared_output_path == output_path else "elimination",
        bound_output_path_present=bool(output_path),
        declaration_only_output_path_present=bool(declared_output_path),
        requested_output_count=len(requested),
        read_result_present=result is not None,
    )
    interaction: ScoutedInteraction = {
        "tool_name": "read_value",
        "read_expression": expression,
        "read_output_path": output_path,
        "read_output_path_source": "declared" if declared_output_path == output_path else "elimination",
        "read_result_shape": type(result).__name__,
    }
    if witnessed:
        interaction["read_result_value"] = witnessed
    if url:
        interaction["source_url"] = url
        interaction["result_url"] = url
    return _record_scout_trajectory_fact(ctx, interaction)


def _unread_requested_output_paths(ctx: AgentContext) -> list[str]:
    """Requested outputs no read has yet answered with a value.

    A read that only gathered candidates still names the path it was aiming at, so counting every
    read as a claim made probing indistinguishable from progress: the turn could inspect a tile
    repeatedly and reach authoring with nothing bound to the output it was asked for.
    """
    # The same set derivation binds and the offer names: rekeying clears a criterion's output_path while it stays
    # graded, so projecting the graded criteria alone loses the path the binder still owes (SKY-13226).
    requested = requested_output_paths_for_derivation(ctx)
    if not requested:
        return []
    claimed = {
        str(interaction.get("read_output_path") or "")
        for interaction in ctx.scout_trajectory
        if interaction.get("tool_name") == "read_value" and interaction.get("read_result_value") is not None
    }
    return sorted(requested - claimed)


async def _evaluate_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    ctx.scout_observation_contract = None
    sensitive_page_refusal = _sensitive_origin_page_action_refusal(ctx)
    matching_registry_is_scrubbable = register_matching_origin_run_redaction_values(ctx)
    if sensitive_page_refusal is not None:
        ctx.pending_scout_read_expression = None
        ctx.pending_scout_read_output_path = None
        return sensitive_page_refusal
    if matching_registry_is_scrubbable:
        result = scrub_secrets_from_structure(ctx, result)
    data = result.get("data")
    if not result.get("ok") or not isinstance(data, dict) or not data:
        return result
    data.pop("sdk_equivalent", None)
    if "url" not in data:
        url, _ = await _resolve_url_title(raw, ctx)
        if url:
            data["url"] = url
    url = str(data.get("url") or "")
    title = str(data.get("title") or "")
    if not title:
        _, title = await _resolve_url_title(raw, ctx)
    observation_step = _record_composition_page_observation(
        ctx,
        source_tool="evaluate",
        url=url,
        title=title,
        observed_data=data,
        append_to_flow=True,
        reached_via="auto",
    )
    if observation_step is not None:
        result["observation_step"] = observation_step
        data["observation_step"] = observation_step
    # The MCP response never echoes the expression; the pre-hook stashed it from the invocation.
    read_expression = ctx.pending_scout_read_expression or ""
    ctx.pending_scout_read_expression = None
    declared_output_path = ctx.pending_scout_read_output_path
    ctx.pending_scout_read_output_path = None
    recorded = _record_scouted_read(
        ctx,
        expression=read_expression,
        data=data,
        url=url,
        declared_output_path=declared_output_path,
    )
    if recorded is not None:
        LOG.info(
            "copilot_scouted_read_recorded",
            read_result_present=bool(recorded.get("read_result_shape")),
            read_output_path_present=bool(recorded.get("read_output_path")),
            witnessed_value_kept=bool(recorded.get("read_result_value")),
            trajectory_len=len(ctx.scout_trajectory),
        )
        claimed_path = str(recorded.get("read_output_path") or "")
        if claimed_path.startswith("output.") and not recorded.get("read_result_value"):
            data["claimed_output_without_a_single_value"] = claimed_path
            data["requested_output_designation_capability"] = requested_output_designation_capability([claimed_path])
            LOG.info(
                "copilot_scouted_read_claimed_output_without_value",
                read_output_path_present=bool(claimed_path),
                read_result_present=bool(recorded.get("read_result_shape")),
            )
    unread_requested = _unread_requested_output_paths(ctx)
    if unread_requested:
        data["requested_outputs_still_unread"] = unread_requested
        LOG.info("copilot_requested_output_unread_after_read", unread_count=len(unread_requested))
    if _copilot_authoring_capability(ctx).code_blocks and _code_only_has_target_page_evidence(data):
        ctx.code_only_target_page_evidence_seen = True
    await _attach_evaluate_page_facts(ctx, result, url=url)
    if data.get("claimed_output_without_a_single_value"):
        candidates = unbound_candidate_relations(ctx.flow_evidence)
        if candidates:
            data["requested_output_designation_candidates"] = [
                {"label": label, "value_text": value} for label, value in candidates
            ]
    await _widen_thin_evaluate_result(ctx, result, url=url)
    return result


_THIN_EVALUATE_RESULT_CHARS = 400


async def _widen_thin_evaluate_result(ctx: AgentContext, result: dict[str, Any], *, url: str) -> None:
    """Carry the page's visible text back when a read returned almost nothing.

    A narrow read that lands on the wrong one of several similarly named elements comes back short
    and plausible, and the next guess is narrow again. A page's whole visible text is usually the
    size of a few such reads, so answering the thin one with it ends the guessing.
    """
    data = result.get("data")
    if not isinstance(data, dict) or "page_visible_text" in data:
        return
    if len(json.dumps(data, default=str)) > _THIN_EVALUATE_RESULT_CHARS:
        return
    evidence = await _safe_composition_evidence(ctx, url, timeout_seconds=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS)
    excerpt = (evidence or {}).get("visible_text_excerpt")
    if isinstance(excerpt, str) and excerpt.strip():
        data["page_visible_text"] = excerpt


async def _scroll_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    sensitive_page_refusal = _sensitive_origin_page_action_refusal(ctx)
    if sensitive_page_refusal is not None:
        return sensitive_page_refusal
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
    sensitive_page_refusal = _sensitive_origin_page_action_refusal(ctx)
    if sensitive_page_refusal is not None:
        return sensitive_page_refusal
    source_url = _consume_scout_source_url(ctx)
    pending_role_name = getattr(ctx, "pending_scout_role_name", None)
    ctx.pending_scout_role_name = None
    pending_role_name_match_count = getattr(ctx, "pending_scout_role_name_match_count", None)
    ctx.pending_scout_role_name_match_count = None
    pending_ambiguous = ctx.pending_scout_ambiguous
    ctx.pending_scout_ambiguous = None
    pending_selector_match_count = getattr(ctx, "pending_scout_selector_match_count", None)
    ctx.pending_scout_selector_match_count = None
    pending_selector_candidates = getattr(ctx, "pending_scout_selector_candidates", None)
    ctx.pending_scout_selector_candidates = None
    ctx.pending_scout_reanchor = None
    if result.get("ok") and result.get("data"):
        data = result["data"]
        selector_candidates = _merge_selector_candidates(
            _selector_candidates_from_tool_data(data), pending_selector_candidates
        )
        selector = _selector_from_tool_data(data)
        url, _ = await _resolve_url_title(raw, ctx)
        _mark_pending_browser_interaction_observation(ctx, tool_name="select_option", url=url)
        result["data"] = {
            "executed_selector": selector,
            "value": data.get("value", ""),
            "url": url,
        }
        role, accessible_name = _prenav_role_name_for_selector(pending_role_name, selector)
        if not (role and accessible_name):
            role, accessible_name = await _resolve_scout_role_name(ctx, selector)
        ambiguous = _prenav_ambiguity_for_selector(pending_ambiguous, selector)
        selector_match_count = (
            pending_selector_match_count[1]
            if isinstance(pending_selector_match_count, tuple)
            and len(pending_selector_match_count) == 2
            and pending_selector_match_count[0] == selector
            else None
        )
        role_name_match_count = (
            pending_role_name_match_count[3]
            if isinstance(pending_role_name_match_count, tuple)
            and len(pending_role_name_match_count) == 4
            and pending_role_name_match_count[:3] == (selector, role, accessible_name)
            else None
        )
        _record_scouted_interaction(
            ctx,
            tool_name="select_option",
            selector=selector,
            selector_candidates=selector_candidates,
            selector_match_count=selector_match_count,
            source_url=source_url,
            result_url=url,
            value=data.get("value", ""),
            role=role,
            accessible_name=accessible_name,
            role_name_match_count=role_name_match_count,
            ambiguous=ambiguous,
        )
        observation_step, page_evidence = await _register_scout_interaction_observation(
            ctx, tool_name="select_option", selector=selector, source_url=source_url, url=url
        )
        _attach_scout_observation_step(
            ctx,
            tool_name="select_option",
            selector=selector,
            observation_step=observation_step,
        )
        if observation_step is not None:
            result["observation_step"] = observation_step
            result["data"]["observation_step"] = observation_step
        if page_evidence is not None:
            _attach_scout_page_summary(ctx, result, page_evidence)
    return result


async def _press_key_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    _clear_pending_browser_interaction_observation(ctx)
    sensitive_page_refusal = _sensitive_origin_page_action_refusal(ctx)
    if sensitive_page_refusal is not None:
        return sensitive_page_refusal
    source_url = _consume_scout_source_url(ctx)
    pending_role_name = getattr(ctx, "pending_scout_role_name", None)
    ctx.pending_scout_role_name = None
    pending_role_name_match_count = getattr(ctx, "pending_scout_role_name_match_count", None)
    ctx.pending_scout_role_name_match_count = None
    pending_ambiguous = getattr(ctx, "pending_scout_ambiguous", None)
    ctx.pending_scout_ambiguous = None
    pending_reanchor = getattr(ctx, "pending_scout_reanchor", None)
    ctx.pending_scout_reanchor = None
    pending_selector_match_count = getattr(ctx, "pending_scout_selector_match_count", None)
    ctx.pending_scout_selector_match_count = None
    pending_selector_candidates = getattr(ctx, "pending_scout_selector_candidates", None)
    ctx.pending_scout_selector_candidates = None
    if result.get("ok") and result.get("data"):
        data = result["data"]
        selector_candidates = _merge_selector_candidates(
            _selector_candidates_from_tool_data(data), pending_selector_candidates
        )
        selector = _selector_from_tool_data(data)
        url, _ = await _resolve_url_title(raw, ctx)
        _mark_pending_browser_interaction_observation(ctx, tool_name="press_key", url=url)
        result["data"] = {
            "key": data.get("key", ""),
            "executed_selector": selector,
            "url": url,
        }
        await _bind_login_credential_for_observed_url(ctx, url, result)
        role, accessible_name = _prenav_role_name_for_selector(pending_role_name, selector)
        if not (role and accessible_name):
            role, accessible_name = await _resolve_scout_role_name(ctx, selector)
        ambiguous = _prenav_ambiguity_for_selector(pending_ambiguous, selector)
        # Consume the optional uniqueness re-anchor without replacing a non-unique observed identity.
        _prenav_role_name_for_selector(pending_reanchor, selector)
        role_name_match_count = (
            pending_role_name_match_count[3]
            if isinstance(pending_role_name_match_count, tuple)
            and len(pending_role_name_match_count) == 4
            and pending_role_name_match_count[:3] == (selector, role, accessible_name)
            else None
        )
        _record_scouted_interaction(
            ctx,
            tool_name="press_key",
            selector=selector,
            selector_candidates=selector_candidates,
            selector_match_count=(
                pending_selector_match_count[1]
                if isinstance(pending_selector_match_count, tuple)
                and len(pending_selector_match_count) == 2
                and pending_selector_match_count[0] == selector
                else None
            ),
            source_url=source_url,
            result_url=url,
            key=data.get("key", ""),
            role=role,
            accessible_name=accessible_name,
            role_name_match_count=role_name_match_count,
            ambiguous=ambiguous,
        )
        observation_step, page_evidence = await _register_scout_interaction_observation(
            ctx, tool_name="press_key", selector=selector, source_url=source_url, url=url
        )
        _attach_scout_observation_step(
            ctx,
            tool_name="press_key",
            selector=selector,
            observation_step=observation_step,
        )
        if observation_step is not None:
            result["observation_step"] = observation_step
            result["data"]["observation_step"] = observation_step
        if page_evidence is not None:
            _attach_scout_page_summary(ctx, result, page_evidence)
    return result


def get_skyvern_mcp_alias_map() -> dict[str, str]:
    return {
        "get_workflow_knowledge": "skyvern_workflow_knowledge",
        "get_block_schema": "skyvern_block_schema",
        "validate_block": "skyvern_block_validate",
        "list_org_workflows": "skyvern_workflow_list",
        "get_org_workflow": "skyvern_workflow_get",
        "navigate_browser": "skyvern_navigate",
        "get_browser_screenshot": "skyvern_screenshot",
        "evaluate": "skyvern_evaluate",
        "click": "skyvern_click",
        "type_text": "skyvern_type",
        "scroll": "skyvern_scroll",
        "console_messages": "skyvern_console_messages",
        "select_option": "skyvern_select_option",
        "press_key": "skyvern_press_key",
        "wait_for_either_state": "skyvern_wait_for_either_state",
        # These frame and tab controls already use their user-facing MCP names.
        "skyvern_frame_list": "skyvern_frame_list",
        "skyvern_frame_switch": "skyvern_frame_switch",
        "skyvern_frame_main": "skyvern_frame_main",
        "skyvern_tab_list": "skyvern_tab_list",
        "skyvern_tab_new": "skyvern_tab_new",
        "skyvern_tab_switch": "skyvern_tab_switch",
        "skyvern_tab_close": "skyvern_tab_close",
        "list_workflow_schedules": "skyvern_schedule_list_for_workflow",
        "get_workflow_schedule": "skyvern_schedule_get",
        "create_workflow_schedule": "skyvern_schedule_create",
        "update_workflow_schedule": "skyvern_schedule_update",
        "enable_workflow_schedule": "skyvern_schedule_enable",
        "disable_workflow_schedule": "skyvern_schedule_disable",
        "delete_workflow_schedule": "skyvern_schedule_delete",
    }


_PENDING_PROPOSAL_SCHEDULE_ERROR = (
    "No schedule was created: this chat has a workflow proposal that is not saved yet, and a schedule "
    "runs the saved workflow. Ask the user to accept the proposal, which saves it, or to reject it to "
    "schedule the saved version as it is; then create the schedule."
)
# The shared tools' hints name tools that are not on Copilot's surface.
_SCHEDULE_HINT_REWRITES = (
    ("Use skyvern_schedule_list to", "Use list_workflow_schedules to"),
    (
        "Use skyvern_workflow_list to find valid workflow IDs.",
        "The workflow open in this chat was not found in this organization.",
    ),
    (", or set exact=true to do a full replace", ""),
)


async def _schedule_parameters_credential_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    # A schedule dispatches unattended runs of the saved definition, so it needs the same approval as a run now.
    parameters = params.get("parameters")
    schedule_id = params.get("workflow_schedule_id")
    has_masked_values = isinstance(parameters, dict) and SECRET_HEADER_MASK in parameters.values()
    if parameters is None or has_masked_values:
        schedule = (
            await app.DATABASE.schedules.get_workflow_schedule_by_id(
                workflow_schedule_id=schedule_id, organization_id=ctx.organization_id
            )
            if isinstance(schedule_id, str)
            else None
        )
        stored = (schedule.parameters if schedule is not None else None) or {}
        if parameters is None:
            parameters = stored
        else:
            # Read results mask every value, so a masked value sent back means "keep the stored one".
            parameters = {
                key: stored[key] if value == SECRET_HEADER_MASK else value
                for key, value in parameters.items()
                if value != SECRET_HEADER_MASK or key in stored
            }
            params["parameters"] = parameters
    credential_ids = _extract_credential_ids_from_tool_value(parameters or {})
    google_reference_ids: list[str] = []
    workflow = (
        await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id=ctx.workflow_permanent_id, organization_id=ctx.organization_id
        )
        if ctx.workflow_permanent_id
        else None
    )
    if workflow is not None:
        credential_ids = list(
            dict.fromkeys(
                credential_ids + _extract_credential_ids_from_workflow_definition(workflow.workflow_definition)
            )
        )
        # ponytail: no same-turn Sheets listing admission like the run path; schedule turns rarely list sheets.
        google_reference_ids = _google_connection_reference_ids(workflow.workflow_definition, None)
    google_approval_blocker = _credential_run_approval_blocker_signal(
        credential_ids,
        ctx.request_policy,
        google_reference_ids=google_reference_ids,
        action="schedule the workflow",
        blocked_tool="workflow_schedule",
    )
    if google_approval_blocker is not None:
        ctx.connected_account_recovery_choices = (
            await _server_verified_google_account_choices(ctx.organization_id) or []
        )
        tool_error = stash_blocker_signal(ctx, google_approval_blocker)
        return {"ok": False, "error": tool_error}
    credential_approval_error = _credential_run_approval_error(credential_ids, ctx.request_policy)
    if credential_approval_error is not None:
        return {"ok": False, "error": credential_approval_error}
    return None


async def _chat_has_pending_proposal(ctx: AgentContext) -> bool:
    if ctx.has_staged_proposal:
        return True
    # The chat row, not the turn's restore outcome: a restore can decline a proposal that stays pending.
    chat_id = ctx.workflow_copilot_chat_id if isinstance(ctx, CopilotContext) else None
    if not chat_id:
        return False
    chat = await app.DATABASE.workflow_params.get_workflow_copilot_chat_by_id(
        organization_id=ctx.organization_id,
        workflow_copilot_chat_id=chat_id,
    )
    return chat is not None and isinstance(chat.proposed_workflow, dict)


async def _create_workflow_schedule_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    if await _chat_has_pending_proposal(ctx):
        return {"ok": False, "error": _PENDING_PROPOSAL_SCHEDULE_ERROR}
    return await _schedule_parameters_credential_pre_hook(params, ctx)


async def _workflow_schedule_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    error = result.get("error")
    if isinstance(error, str):
        for raw_hint, copilot_hint in _SCHEDULE_HINT_REWRITES:
            error = error.replace(raw_hint, copilot_hint)
        result["error"] = error
    _mask_schedule_parameter_values(result.get("data"))
    return result


def _mask_schedule_parameter_values(value: object) -> None:
    # Stored inputs can hold secrets this chat never saw, and the scrubber only knows values it has seen.
    if isinstance(value, dict):
        if "workflow_schedule_id" in value and isinstance(value.get("parameters"), dict):
            value["parameters"] = dict.fromkeys(value["parameters"], SECRET_HEADER_MASK)
        for item in value.values():
            _mask_schedule_parameter_values(item)
    elif isinstance(value, list):
        for item in value:
            _mask_schedule_parameter_values(item)


def _workflow_schedule_overlay(
    description: str,
    *,
    hide_params: frozenset[str] = frozenset(),
    pre_hook: PreHook | None = None,
    mutates: bool = True,
) -> SchemaOverlay:
    return SchemaOverlay(
        description=description,
        hide_params=hide_params | {"workflow_permanent_id"},
        binds_chat_workflow=True,
        requires_run_authority=mutates,
        pre_hook=pre_hook,
        post_hook=_workflow_schedule_post_hook,
    )


_EVALUATE_BASE_DESCRIPTION = (
    "Execute JavaScript in the browser and return the result. Use it to inspect DOM state and read "
    "values. JavaScript run here can also change the page, but only click, type_text, select_option "
    "and press_key record a scouted interaction, so a change made through this tool leaves nothing to "
    "author from -- act with those tools and read with this one."
)
_WORKFLOW_KNOWLEDGE_DESCRIPTION = (
    "Read authoritative Skyvern workflow concepts and authoring guidance. Use this before answering "
    "questions about workflow structure, parameters, execution, authoring patterns, or block selection. "
    "Common topic IDs are workflow_parameters, parameter_templating, workflow_execution_flow, "
    "choosing_a_block, common_patterns, best_practices, and code_block_runtime (the builtins, module "
    "shims, and helpers a code block's Python may use); omit topics to list every available ID. "
    "Request only the relevant sections. For exact fields of a specific block type, use "
    "get_block_schema instead."
)
# Scout-ACT framing: a download (or row-expand / post-login) affordance exposes its terminal
# target only once its page is reached. The model reaches that page with navigate/click and
# observes it here -- evaluate records no interaction -- so the model can author the download step.
_EVALUATE_SCOUT_ACT_DESCRIPTION = (
    _EVALUATE_BASE_DESCRIPTION
    + " Some affordances (a download, a row-expand, a post-login area) only expose their target "
    "once the page holding them is reached. Use this tool to OBSERVE that page; reach it with the "
    "navigate/click tools first. For a download, observe the page that "
    "exposes the download control and capture a stable selector, then author the terminal download "
    "step from the code-block schema contract."
)


_EVALUATE_OVERLAY_TIMEOUT_SECONDS = outer_cap_seconds(DEFAULT_ACTION_TIMEOUT_MS)


def _normalized_authoring_capability(
    capability: AuthoringCapability | BlockAuthoringPolicy | str | None,
) -> AuthoringCapability:
    if isinstance(capability, AuthoringCapability):
        return capability
    return authoring_capability_from_policy(capability)


def _evaluate_overlay_description(
    capability: AuthoringCapability | BlockAuthoringPolicy | str | None = None,
) -> str:
    if _normalized_authoring_capability(capability).code_blocks:
        return _EVALUATE_SCOUT_ACT_DESCRIPTION
    return _EVALUATE_BASE_DESCRIPTION


def _block_schema_banned_types_note(
    capability: AuthoringCapability | BlockAuthoringPolicy | str | None = None,
) -> str:
    """Name the rejected types from the same table the schema pre-hook rejects on. The set depends on
    the turn's authoring capability, which a static prompt line cannot track."""
    banned = _banned_block_types_for_capability(_normalized_authoring_capability(capability))
    return f"Unavailable for this turn and rejected on request: {', '.join(sorted(banned))}."


def _build_skyvern_mcp_overlays(
    capability: AuthoringCapability | BlockAuthoringPolicy | str | None = None,
) -> dict[str, SchemaOverlay]:
    return {
        "get_workflow_knowledge": SchemaOverlay(
            description=_WORKFLOW_KNOWLEDGE_DESCRIPTION,
            description_suffix=_block_schema_banned_types_note(capability),
            post_hook=_get_workflow_knowledge_post_hook,
        ),
        "get_block_schema": SchemaOverlay(
            description_suffix=_block_schema_banned_types_note(capability),
            pre_hook=_get_block_schema_pre_hook,
            post_hook=_get_block_schema_post_hook,
        ),
        "validate_block": SchemaOverlay(pre_hook=_validate_block_pre_hook),
        "list_org_workflows": SchemaOverlay(
            description=(
                "Search this organization's saved workflows by title, folder, or parameter name. Reach for it "
                "when the user refers to one of their existing workflows ('like my X workflow', 'the one I "
                "built for Y'), or before building for a site the org may already automate, so the new build "
                "reuses the org's proven parameters, TOTP wiring and loop style. Each match carries "
                "workflow_permanent_id (wpid_...), workflow_id, title, version, status and description. Pass "
                "the workflow_permanent_id -- not workflow_id -- to get_org_workflow for the full definition."
            ),
            hide_params=frozenset({"query"}),
        ),
        "get_org_workflow": SchemaOverlay(
            description=(
                "Read one saved workflow's full definition. workflow_id must be the wpid_... value that "
                "list_org_workflows returns as workflow_permanent_id; pass version=<n> for an earlier saved "
                "version. Use it to copy an existing workflow's structure into a new build, or to answer "
                "questions about a previous saved version of the workflow open in this chat (version=<n-1>). "
                "Read-only; it does not change the current draft."
            ),
        ),
        "navigate_browser": SchemaOverlay(
            description=(
                "Navigate the debug browser to a URL. "
                "Use this to reset browser state or navigate to a starting page before running blocks."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            pre_hook=_navigate_pre_hook,
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
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            pre_hook=_screenshot_pre_hook,
            post_hook=_screenshot_post_hook,
        ),
        "evaluate": SchemaOverlay(
            description=_evaluate_overlay_description(capability),
            hide_params=frozenset({"session_id", "cdp_url"}),
            copilot_params={
                "output_path": {
                    "type": "string",
                    "description": (
                        "The requested output this read fills, such as 'output.visitors'. Set it "
                        "whenever the expression reads a value the user asked for, so the workflow "
                        "returns that value under that name. Read each requested value in its own "
                        "call rather than one expression returning several. Naming a path says the "
                        "expression evaluates to that one value, which is what lets the workflow "
                        "find it again; an expression that gathers candidates is exploration, so "
                        "leave the path off and name it on the follow-up read of the value itself."
                    ),
                },
                BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM,
            },
            requires_browser=True,
            redacts_sensitive_origin_structured_result=True,
            timeout=_EVALUATE_OVERLAY_TIMEOUT_SECONDS,
            pre_hook=_evaluate_pre_hook,
            post_hook=_evaluate_post_hook,
        ),
        "click": SchemaOverlay(
            description=(
                "Click an element in the browser by CSS selector. The click is instant and "
                "deterministic. Derive the selector from page evidence. When a shared "
                "class matches many elements (e.g. one button per result row), scope the "
                "selector to the specific item (its container or a unique attribute). If a "
                "selector does not resolve, inspect the page again and derive a better one. "
                "IMPORTANT: jQuery pseudo-selectors like :contains(), :eq(), :first, "
                ":visible are NOT valid CSS. Use standard selectors: "
                "'button.download', 'a[href*=\"pdf\"]', '#submit-btn'."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "button", "click_count", "intent"}),
            forced_args={"selector_mode": "direct"},
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            redacts_sensitive_origin_structured_result=True,
            pre_hook=_click_pre_hook,
            post_hook=_click_post_hook,
        ),
        "type_text": SchemaOverlay(
            description=(
                "Type text into an input element with Skyvern's active input event strategy while "
                "targeting the supplied CSS selector directly. Derive the selector from page evidence; "
                "if it does not resolve, "
                "inspect the page again and derive a better one. "
                "Optionally clear the field first. Use this for form filling. "
                "NEVER type inline passwords, API keys, tokens, cookies, TOTP/OTP "
                "codes, private keys, or other raw credentials/secrets received in "
                "chat. Ask the user to store the value as a saved credential and "
                "reply with its name; do not type or submit the raw value."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "delay", "intent"}),
            forced_args={"selector_mode": "direct"},
            required_overrides=["text"],
            arg_transforms={"clear_first": "clear"},
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            redacts_sensitive_origin_structured_result=True,
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
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            redacts_sensitive_origin_structured_result=True,
            pre_hook=_scroll_pre_hook,
            post_hook=_scroll_post_hook,
        ),
        "console_messages": SchemaOverlay(
            description=(
                "Read console log messages from the browser. "
                "Use level='error' to find JavaScript errors. "
                "This is a read-only diagnostic tool."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            pre_hook=_sensitive_origin_page_pre_hook,
            post_hook=_sensitive_origin_page_post_hook,
        ),
        "select_option": SchemaOverlay(
            description=(
                "Select an option from a <select> dropdown. Provide the value to select and a "
                "CSS selector to target the element precisely. For free-text inputs, use "
                "type_text instead."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "timeout", "intent"}),
            forced_args={"selector_mode": "direct"},
            required_overrides=["value"],
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            redacts_sensitive_origin_structured_result=True,
            pre_hook=_select_option_pre_hook,
            post_hook=_select_option_post_hook,
        ),
        "press_key": SchemaOverlay(
            description=(
                "Press a keyboard key (Enter, Tab, Escape, ArrowDown, etc.). "
                "Optionally focus an element first via CSS selector. "
                "Use for form submission, tab navigation, or closing dialogs."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "intent"}),
            required_overrides=["key"],
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            redacts_sensitive_origin_structured_result=True,
            pre_hook=_press_key_pre_hook,
            post_hook=_press_key_post_hook,
        ),
        "wait_for_either_state": SchemaOverlay(
            hide_params=frozenset({"session_id", "cdp_url"}),
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            redacts_sensitive_origin_structured_result=True,
            pre_hook=_sensitive_origin_page_action_pre_hook,
            post_hook=_wait_for_either_state_post_hook,
        ),
        "skyvern_frame_list": SchemaOverlay(
            hide_params=frozenset({"session_id", "cdp_url"}),
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            pre_hook=_sensitive_origin_page_pre_hook,
            post_hook=_sensitive_origin_page_post_hook,
        ),
        "skyvern_frame_switch": SchemaOverlay(
            hide_params=frozenset({"session_id", "cdp_url"}),
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            pre_hook=_sensitive_origin_page_pre_hook,
            post_hook=_sensitive_origin_page_post_hook,
        ),
        "skyvern_frame_main": SchemaOverlay(
            hide_params=frozenset({"session_id", "cdp_url"}),
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            pre_hook=_sensitive_origin_page_pre_hook,
            post_hook=_sensitive_origin_page_post_hook,
        ),
        "skyvern_tab_list": SchemaOverlay(
            description=(
                "List the open browser tabs: each entry carries tab_id, index, url, title and is_active, "
                "and the result names active_tab_id. Listing changes nothing."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            pre_hook=_sensitive_origin_page_pre_hook,
            post_hook=_sensitive_origin_page_post_hook,
        ),
        "skyvern_tab_new": SchemaOverlay(
            description=(
                "Open a new browser tab, navigating it to url when one is given, and make it the active tab; "
                "the result carries the new tab's tab_id, url and title. Other tabs stay open. The browser "
                f"holds at most {settings.BROWSER_MAX_PAGES_NUMBER} tabs; at that count the call fails and says so."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            pre_hook=_tab_new_pre_hook,
            post_hook=_sensitive_origin_page_post_hook,
        ),
        "skyvern_tab_switch": SchemaOverlay(
            description=(
                "Make the tab named by tab_id (from skyvern_tab_list) or index the active tab that the other "
                "browser tools act on; the result reports the tab that is now active."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            pre_hook=_tab_switch_pre_hook,
            post_hook=_sensitive_origin_page_post_hook,
        ),
        # Only an active sensitive run refuses: the result carries no page fact, and closing tabs is
        # how a withheld multi-tab browser gets back to the one tab a fresh navigation can clear.
        "skyvern_tab_close": SchemaOverlay(
            description=(
                "Close one browser tab, the one named by tab_id (from skyvern_tab_list) or index, or the active "
                "tab when neither is given; the result carries closed_tab_id and remaining_tabs, and "
                "skyvern_tab_list reports which tab is active afterwards."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
            requires_browser=True,
            pre_hook=_tab_close_pre_hook,
        ),
        "list_workflow_schedules": _workflow_schedule_overlay(
            "List the schedules of the workflow open in this chat, with each wfs_ ID. Check it before creating "
            "or changing a schedule, and again afterwards to confirm.",
            mutates=False,
        ),
        "get_workflow_schedule": _workflow_schedule_overlay(
            "Read one schedule of the workflow open in this chat by wfs_ ID, with its next run times.",
            mutates=False,
        ),
        "create_workflow_schedule": _workflow_schedule_overlay(
            "Schedule the saved workflow open in this chat. Cadence and IANA timezone must come from the user; "
            "if either is missing, ask rather than assume UTC or local time. Leave name unset unless the user "
            "gave one. Ask before duplicating a listed schedule. Cron cannot express an elapsed interval such "
            "as 'every 36 hours'; do not approximate one. In your reply, give the saved wfs_ ID, timezone, "
            "enabled state and next run from the result.",
            pre_hook=_create_workflow_schedule_pre_hook,
        ),
        "update_workflow_schedule": _workflow_schedule_overlay(
            "Change a schedule by wfs_ ID, passing only the fields that change; parameters, name and paused "
            "state are kept. Send name or clear_name only when the user asks to rename it, even if the name "
            "mentions the old time. Parameter values read back as ***; send *** for a key to keep its stored "
            "value. Ask which one when several could match.",
            hide_params=frozenset({"exact"}),
            pre_hook=_schedule_parameters_credential_pre_hook,
        ),
        "enable_workflow_schedule": _workflow_schedule_overlay(
            "Resume a paused schedule by wfs_ ID. Ask which one when several could match.",
            pre_hook=_schedule_parameters_credential_pre_hook,
        ),
        "disable_workflow_schedule": _workflow_schedule_overlay(
            "Pause a schedule by wfs_ ID without deleting it. Ask which one when several could match."
        ),
        "delete_workflow_schedule": _workflow_schedule_overlay(
            "Delete a schedule by wfs_ ID; irreversible. Pass force=true only when the user clearly asked to "
            "remove that schedule, then list schedules to confirm it is gone."
        ),
    }

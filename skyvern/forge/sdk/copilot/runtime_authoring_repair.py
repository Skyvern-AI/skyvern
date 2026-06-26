from __future__ import annotations

import re
from typing import Any, TypeGuard

from skyvern.forge.sdk.copilot.composition_evidence import has_bounded_page_schema
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, normalize_block_authoring_policy
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext
from skyvern.forge.sdk.copilot.failure_tracking import ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY
from skyvern.forge.sdk.copilot.request_policy import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.run_outcome import trusted_terminal_challenge_category_name
from skyvern.forge.sdk.copilot.workflow_credential_utils import url_origin

_RUNTIME_AUTHORING_REASON_CODE = "runtime_block_failure"
_RUNTIME_SUMMARY_MAX_CHARS = 120
_RUNTIME_SUMMARY_MAX_ITEMS = 5
_INSPECT_PAGE_SOURCE_TOOL = "inspect_page_for_composition"


def is_runtime_authoring_repair_context(repair_context: object) -> TypeGuard[CodeAuthoringRepairContext]:
    return (
        isinstance(repair_context, CodeAuthoringRepairContext)
        and repair_context.reason_code == _RUNTIME_AUTHORING_REASON_CODE
    )


def clear_runtime_authoring_repair_context(copilot_ctx: Any) -> None:
    copilot_ctx.pending_code_authoring_runtime_repair_context = None
    if is_runtime_authoring_repair_context(getattr(copilot_ctx, "last_code_authoring_repair_context", None)):
        copilot_ctx.last_code_authoring_repair_context = None


def _bounded_runtime_text(value: Any, max_chars: int = _RUNTIME_SUMMARY_MAX_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    text = redact_raw_secrets_for_prompt(" ".join(value.split()))
    return text[:max_chars]


def _runtime_failure_class(reason: str) -> str:
    reason_lower = reason.lower()
    if "timeout" in reason_lower and any(token in reason_lower for token in ("locator", "selector", "element")):
        return "timeout_waiting_for_selector"
    if "not found" in reason_lower and any(token in reason_lower for token in ("locator", "selector", "element")):
        return "selector_not_found"
    normalized = re.sub(r"[^a-z0-9]+", "_", reason_lower).strip("_")
    return normalized[:80].strip("_") or "runtime_failure"


def _origin_from_runtime_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return url_origin(value)


def _runtime_summary_entry(entry: Any, keys: tuple[str, ...]) -> str:
    if not isinstance(entry, dict):
        return _bounded_runtime_text(entry)
    parts = [
        _bounded_runtime_text(entry.get(key), 60)
        if not isinstance(entry.get(key), bool)
        else ("disabled" if entry.get(key) is True else "enabled")
        for key in keys
    ]
    return _bounded_runtime_text(" ".join(part for part in parts if part))


def _runtime_summary_list(value: Any, keys: tuple[str, ...]) -> list[str]:
    if not isinstance(value, list):
        return []
    summaries: list[str] = []
    for entry in value[:_RUNTIME_SUMMARY_MAX_ITEMS]:
        summary = _runtime_summary_entry(entry, keys)
        if summary:
            summaries.append(summary)
    return summaries


def _post_run_terminal_page_evidence(evidence: dict[str, Any]) -> bool:
    if evidence.get("observed_after_workflow_run") is not True:
        return False
    challenge_state = evidence.get("challenge_state")
    if isinstance(challenge_state, dict):
        if challenge_state.get("gates_submit_controls") is True:
            return True
        if (
            challenge_state.get("detected") is True
            and challenge_state.get("requires_human_verification") is True
            and _runtime_summary_list(evidence.get("forms"), ("label", "selector"))
        ):
            return True
    indicators = evidence.get("anti_bot_indicators")
    has_indicators = isinstance(indicators, list) and any(isinstance(item, str) and item.strip() for item in indicators)
    controls = evidence.get("challenge_controls")
    has_controls = isinstance(controls, list) and any(isinstance(item, dict) for item in controls)
    return has_indicators and has_controls


def _first_runtime_failed_block(data: dict[str, Any]) -> dict[str, Any] | None:
    blocks = data.get("blocks")
    if not isinstance(blocks, list):
        return None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        status = str(block.get("status") or "").lower()
        if status in {"failed", "terminated", "canceled", "timed_out"}:
            return block
    return None


def record_pending_runtime_authoring_repair_context(copilot_ctx: Any, result: dict[str, Any]) -> None:
    if bool(result.get("ok", False)):
        clear_runtime_authoring_repair_context(copilot_ctx)
        return
    data = result.get("data")
    if not isinstance(data, dict):
        clear_runtime_authoring_repair_context(copilot_ctx)
        return
    run_id = data.get("workflow_run_id")
    if not isinstance(run_id, str) or not run_id:
        clear_runtime_authoring_repair_context(copilot_ctx)
        return
    block = _first_runtime_failed_block(data)
    failure_reason = ""
    block_label = _bounded_runtime_text(data.get("frontier_start_label"), 80)
    failed_block_status = _bounded_runtime_text(data.get("overall_status"), 40)
    if block is not None:
        block_label = _bounded_runtime_text(block.get("label"), 80) or block_label
        failed_block_status = _bounded_runtime_text(block.get("status"), 40) or failed_block_status
        failure_reason = _bounded_runtime_text(block.get("failure_reason"), 240)
    failure_reason = failure_reason or _bounded_runtime_text(data.get("failure_reason"), 240)
    failure_reason = failure_reason or _bounded_runtime_text(result.get("error"), 240)
    if not block_label or not failure_reason:
        clear_runtime_authoring_repair_context(copilot_ctx)
        return
    if is_runtime_authoring_repair_context(getattr(copilot_ctx, "last_code_authoring_repair_context", None)):
        copilot_ctx.last_code_authoring_repair_context = None
    copilot_ctx.pending_code_authoring_runtime_repair_context = CodeAuthoringRepairContext(
        block_label=block_label,
        reason_code=_RUNTIME_AUTHORING_REASON_CODE,
        runtime_failure_reason=failure_reason,
        runtime_failure_class=_runtime_failure_class(failure_reason),
        failed_block_status=failed_block_status or None,
        workflow_run_id=run_id,
        repair_instruction=(
            "adapt the next code block to the observed page state and do not re-emit the same failing selector "
            "or name path."
        ),
    )


def _authority_requires_ask(copilot_ctx: Any) -> bool:
    authority = getattr(getattr(copilot_ctx, "turn_intent", None), "authority", None)
    return getattr(authority, "requires_user_input", False) or getattr(authority, "may_update_workflow", True) is False


def _policy_allows_runtime_authoring_repair(copilot_ctx: Any) -> bool:
    return normalize_block_authoring_policy(getattr(copilot_ctx, "block_authoring_policy", None)) == (
        BlockAuthoringPolicy.CODE_ONLY_BROWSER
    )


def _error_text_requires_stop(copilot_ctx: Any, data: dict[str, Any], result: dict[str, Any] | None = None) -> bool:
    if getattr(copilot_ctx, "last_test_non_retriable_nav_error", None):
        return True
    text_values = [data.get("failure_reason"), data.get("skip_reason")]
    if result is not None:
        text_values.append(result.get("error"))
    text = " ".join(str(value).lower() for value in text_values if value)
    return (
        "browser session not found" in text
        or "no browser context" in text
        or ("session not found" in text and "browser" in text)
        or ("404" in text and "browser session" in text)
    )


def _error_text_requires_ask(data: dict[str, Any], result: dict[str, Any] | None = None) -> bool:
    text_values = [data.get("failure_reason"), data.get("skip_reason"), data.get("failure_type")]
    if result is not None:
        text_values.append(result.get("error"))
    text = " ".join(str(value).lower() for value in text_values if value)
    return (
        "workflow_credential_inputs_unbound" in text
        or "credential inputs unbound" in text
        or "required credentials are not configured" in text
        or "missing_credential_or_init" in text
    )


def _pending_state_has_stop_or_ask_precedence(copilot_ctx: Any, pending: CodeAuthoringRepairContext) -> bool:
    data = {
        "failure_reason": pending.runtime_failure_reason,
        "skip_reason": pending.runtime_failure_reason,
        "failure_type": pending.runtime_failure_class,
    }
    if _authority_requires_ask(copilot_ctx):
        return True
    return _error_text_requires_stop(copilot_ctx, data) or _error_text_requires_ask(data)


def _result_has_terminal_or_ask_precedence(copilot_ctx: Any, data: dict[str, Any], result: dict[str, Any]) -> bool:
    if _authority_requires_ask(copilot_ctx):
        return True
    if _error_text_requires_stop(copilot_ctx, data, result):
        return True
    if _error_text_requires_ask(data, result):
        return True
    if data.get("active_run_terminal_evidence_detected") is True:
        return True
    if data.get("skip_reason") == "workflow_credential_inputs_unbound":
        return True
    if data.get("failure_type") in {"schema_incompatibility", "missing_credential_or_init"}:
        return True
    categories = data.get("failure_categories")
    if not isinstance(categories, list):
        return False
    for entry in categories:
        if not isinstance(entry, dict):
            continue
        category = entry.get("category")
        if category in {
            "UNRECOVERABLE_TOOL_ERROR",
            ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY,
            "ANTI_BOT_DETECTION",
        }:
            return True
        if trusted_terminal_challenge_category_name(entry):
            return True
    return False


def _matching_bounded_post_run_inspection(
    copilot_ctx: Any, pending: CodeAuthoringRepairContext
) -> dict[str, Any] | None:
    evidence = getattr(copilot_ctx, "composition_page_evidence", None)
    if not isinstance(evidence, dict):
        return None
    if evidence.get("source_tool") != _INSPECT_PAGE_SOURCE_TOOL:
        clear_runtime_authoring_repair_context(copilot_ctx)
        return None
    if evidence.get("observed_after_workflow_run") is not True:
        clear_runtime_authoring_repair_context(copilot_ctx)
        return None
    run_id = evidence.get("workflow_run_id")
    if not isinstance(run_id, str) or run_id != pending.workflow_run_id:
        clear_runtime_authoring_repair_context(copilot_ctx)
        return None
    if not has_bounded_page_schema(evidence):
        clear_runtime_authoring_repair_context(copilot_ctx)
        return None
    if _post_run_terminal_page_evidence(evidence):
        clear_runtime_authoring_repair_context(copilot_ctx)
        return None
    return evidence


def finalize_runtime_authoring_repair_context_from_page_observation(
    copilot_ctx: Any,
) -> CodeAuthoringRepairContext | None:
    pending = getattr(copilot_ctx, "pending_code_authoring_runtime_repair_context", None)
    if not is_runtime_authoring_repair_context(pending):
        return None
    if not _policy_allows_runtime_authoring_repair(copilot_ctx) or _pending_state_has_stop_or_ask_precedence(
        copilot_ctx, pending
    ):
        clear_runtime_authoring_repair_context(copilot_ctx)
        return None
    evidence = _matching_bounded_post_run_inspection(copilot_ctx, pending)
    if evidence is None:
        return None
    current_url = evidence.get("current_url") or evidence.get("inspected_url")
    page_title = evidence.get("page_title") or evidence.get("title")
    finalized = pending.model_copy(
        update={
            "current_origin": _origin_from_runtime_url(current_url),
            "current_url_present": isinstance(current_url, str) and bool(current_url.strip()),
            "current_title_present": isinstance(page_title, str) and bool(page_title.strip()),
            "page_evidence_source": _bounded_runtime_text(evidence.get("source_tool"), 80) or None,
            "observed_after_workflow_run": True,
            "page_form_summaries": _runtime_summary_list(evidence.get("forms"), ("label", "selector")),
            "page_result_summaries": _runtime_summary_list(evidence.get("result_containers"), ("label", "text")),
            "page_action_summaries": _runtime_summary_list(
                evidence.get("navigation_targets"), ("label", "selector", "disabled")
            ),
            "page_challenge_summaries": _runtime_summary_list(
                evidence.get("challenge_controls"), ("text", "selector", "disabled")
            ),
        }
    )
    copilot_ctx.last_code_authoring_repair_context = finalized
    copilot_ctx.pending_code_authoring_runtime_repair_context = None
    return finalized


def inject_runtime_authoring_repair_context(copilot_ctx: Any, result: dict[str, Any]) -> None:
    data = result.get("data")
    if not isinstance(data, dict):
        return
    if _result_has_terminal_or_ask_precedence(copilot_ctx, data, result):
        clear_runtime_authoring_repair_context(copilot_ctx)
        data.pop("authoring_repair_context", None)
        return
    repair_context = finalize_runtime_authoring_repair_context_from_page_observation(copilot_ctx)
    if repair_context is None:
        pending = getattr(copilot_ctx, "pending_code_authoring_runtime_repair_context", None)
        if not is_runtime_authoring_repair_context(pending):
            return
        if not _policy_allows_runtime_authoring_repair(copilot_ctx) or _pending_state_has_stop_or_ask_precedence(
            copilot_ctx, pending
        ):
            clear_runtime_authoring_repair_context(copilot_ctx)
            data.pop("authoring_repair_context", None)
            return
        repair_context = pending
        copilot_ctx.last_code_authoring_repair_context = repair_context
    data["authoring_repair_context"] = repair_context.model_dump(mode="json")

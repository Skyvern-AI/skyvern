from __future__ import annotations

import re
from typing import Any, TypeGuard
from urllib.parse import urlsplit, urlunsplit

import structlog
from pydantic import ValidationError

from skyvern.forge.sdk.copilot.challenge_evidence import (
    RUNTIME_SOLVABLE_CHALLENGE_KINDS,
    ChallengeKind,
    interactive_challenge_controls,
    is_carrier_backed_category_entry,
    typed_challenge_kind,
)
from skyvern.forge.sdk.copilot.composition_evidence import has_bounded_page_schema, model_visible_composition_evidence
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, normalize_block_authoring_policy
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, PageObstruction
from skyvern.forge.sdk.copilot.output_contracts import code_block_available_contracts_by_label
from skyvern.forge.sdk.copilot.request_policy import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.run_outcome import trusted_terminal_challenge_category_name
from skyvern.forge.sdk.copilot.workflow_credential_utils import url_origin

LOG = structlog.get_logger()

_RUNTIME_AUTHORING_REASON_CODE = "runtime_block_failure"
_MISSING_OUTPUT_DEPENDENCY_REASON_CODE = "runtime_missing_output_dependency"
_RUNTIME_SUMMARY_MAX_CHARS = 120
_RUNTIME_SUMMARY_MAX_ITEMS = 5
_INSPECT_PAGE_SOURCE_TOOL = "inspect_page_for_composition"
_OBSTRUCTION_KEYS = ("kind", "text", "visual_location")
_OBSTRUCTION_CONTROL_KEYS = ("text",)
_OBSTRUCTION_FIELD_MAX_CHARS = 160
OBSTRUCTION_SUMMARY_MAX_CHARS = 1200
_NO_DISMISS_CONTROL_SUMMARY = "obstruction present, no dismiss control found in page evidence"
_KEY_ERROR_RE = re.compile(r"KeyError(?:\s*:|\()\s*['\"]([^'\"]+)['\"]")


def is_runtime_authoring_repair_context(repair_context: object) -> TypeGuard[CodeAuthoringRepairContext]:
    return isinstance(repair_context, CodeAuthoringRepairContext) and repair_context.reason_code in {
        _RUNTIME_AUTHORING_REASON_CODE,
        _MISSING_OUTPUT_DEPENDENCY_REASON_CODE,
    }


def clear_runtime_authoring_repair_context(copilot_ctx: Any) -> None:
    copilot_ctx.pending_code_authoring_runtime_repair_context = None
    if is_runtime_authoring_repair_context(getattr(copilot_ctx, "last_code_authoring_repair_context", None)):
        copilot_ctx.last_code_authoring_repair_context = None


def _bounded_runtime_text(value: Any, max_chars: int = _RUNTIME_SUMMARY_MAX_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    text = redact_raw_secrets_for_prompt(" ".join(value.split()))
    return text[:max_chars]


def _missing_key_from_key_error(reason: str) -> str | None:
    match = _KEY_ERROR_RE.search(reason)
    if match is None:
        return None
    key = match.group(1).strip()
    return key if key else None


def _missing_output_dependency_context(
    *,
    copilot_ctx: Any,
    block_label: str,
    failed_block_status: str | None,
    failure_reason: str,
    run_id: str,
) -> CodeAuthoringRepairContext | None:
    missing_key = _missing_key_from_key_error(failure_reason)
    workflow_yaml = getattr(copilot_ctx, "workflow_yaml", None)
    if not missing_key or not isinstance(workflow_yaml, str) or not workflow_yaml.strip():
        return None
    contract = code_block_available_contracts_by_label(workflow_yaml).get(block_label)
    if contract is None:
        return None
    if not missing_key.endswith("_output"):
        return None
    if missing_key in contract.available_output_keys:
        return None
    if missing_key in contract.declared_workflow_parameter_keys:
        return None
    if missing_key in contract.available_binding_keys:
        return None
    if missing_key not in contract.parameter_keys:
        return None
    available_output_keys = list(contract.available_output_keys)
    return CodeAuthoringRepairContext(
        block_label=block_label,
        reason_code=_MISSING_OUTPUT_DEPENDENCY_REASON_CODE,
        parameter_keys=list(contract.parameter_keys),
        available_parameter_keys=list(contract.available_binding_keys),
        binding_candidates=available_output_keys,
        runtime_failure_reason=failure_reason,
        output_dependency_failure_class="missing_prior_block_output",
        missing_output_key=missing_key,
        available_output_keys=available_output_keys,
        current_block_parameter_keys=list(contract.parameter_keys),
        failed_block_status=failed_block_status or None,
        workflow_run_id=run_id,
        repair_instruction=(
            "repair the missing prior block output dependency by binding to an actual available prior output key "
            "or changing the producing/current code block so the dependency is real; do not invent a workflow "
            "parameter for this missing output key."
        ),
    )


def _origin_from_runtime_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return url_origin(value)


def _safe_runtime_page_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    redacted = redact_raw_secrets_for_prompt(value)
    try:
        parsed = urlsplit(redacted)
    except ValueError:
        return None
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    safe_url = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    return _bounded_runtime_text(safe_url, 160) or None


def _runtime_summary_entry(
    entry: Any,
    keys: tuple[str, ...],
    field_max_chars: int = 60,
    summary_max_chars: int = _RUNTIME_SUMMARY_MAX_CHARS,
) -> str:
    if not isinstance(entry, dict):
        return _bounded_runtime_text(entry)
    parts = [
        _bounded_runtime_text(entry.get(key), field_max_chars)
        if not isinstance(entry.get(key), bool)
        else ("disabled" if entry.get(key) is True else "enabled")
        for key in keys
    ]
    return _bounded_runtime_text(" ".join(part for part in parts if part), summary_max_chars)


def _runtime_summary_list(value: Any, keys: tuple[str, ...]) -> list[str]:
    if not isinstance(value, list):
        return []
    summaries: list[str] = []
    for entry in value[:_RUNTIME_SUMMARY_MAX_ITEMS]:
        summary = _runtime_summary_entry(entry, keys)
        if summary:
            summaries.append(summary)
    return summaries


def _runtime_form_summaries(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    summaries: list[str] = []
    for form in value:
        if not isinstance(form, dict):
            continue
        for field in form.get("fields") or []:
            summary = _runtime_summary_entry(field, ("label", "type"))
            if summary:
                summaries.append(summary)
        for control in form.get("submit_controls") or []:
            summary = _runtime_summary_entry(control, ("text", "disabled"))
            if summary:
                summaries.append(summary)
    return summaries[:_RUNTIME_SUMMARY_MAX_ITEMS]


def _runtime_result_summaries(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    summaries: list[str] = []
    for container in value:
        if not isinstance(container, dict):
            continue
        primary = _runtime_summary_entry(container, ("text_excerpt",))
        if primary:
            summaries.append(primary)
        for row in container.get("sample_rows") or []:
            summary = _bounded_runtime_text(row, 80)
            if summary:
                summaries.append(summary)
                break
    return summaries[:_RUNTIME_SUMMARY_MAX_ITEMS]


def _raw_obstruction_entries(evidence: dict[str, Any]) -> tuple[list[Any], list[str]]:
    if "page_obstructions" in evidence:
        page_obstructions = evidence.get("page_obstructions")
        if isinstance(page_obstructions, list):
            return page_obstructions, []
        return [], ["failure.page_state.obstructions omitted: canonical page_obstructions was malformed."]
    modal_overlays = evidence.get("modal_overlays")
    if not isinstance(modal_overlays, list):
        return [], []
    return (
        [
            {"visible_controls": overlay.get("dismiss_controls") or []}
            for overlay in modal_overlays
            if isinstance(overlay, dict)
        ],
        [],
    )


def _typed_runtime_page_obstructions(evidence: Any) -> tuple[list[PageObstruction], list[str]]:
    if not isinstance(evidence, dict):
        return [], []
    raw_entries, notices = _raw_obstruction_entries(evidence)
    obstructions: list[PageObstruction] = []
    malformed = 0
    for entry in raw_entries:
        if not isinstance(entry, dict):
            malformed += 1
            continue
        try:
            obstructions.append(PageObstruction.model_validate(model_visible_composition_evidence(entry)))
        except ValidationError:
            malformed += 1
    if malformed:
        notices.append(f"failure.page_state.obstructions omitted: {malformed} malformed item(s).")
    return obstructions, notices


def _has_page_obstruction(evidence: dict[str, Any]) -> bool:
    obstructions, _ = _typed_runtime_page_obstructions(evidence)
    return bool(obstructions)


def repair_page_evidence_is_admissible(evidence: dict[str, Any]) -> bool:
    """A packet whose only structured content is an obstruction carries no bounded page schema, yet
    it is the entire repair signal for a click the overlay intercepted."""
    return has_bounded_page_schema(evidence) or _has_page_obstruction(evidence)


def _joined_obstruction_summary(obstruction: str, control: str) -> str:
    """The control's selector is the repair-critical tail, so the obstruction prefix absorbs the
    whole shortfall instead of letting the shared cap clip the selector off the end."""
    if not obstruction:
        return control[:OBSTRUCTION_SUMMARY_MAX_CHARS]
    budget = OBSTRUCTION_SUMMARY_MAX_CHARS - len(control) - 1
    if budget < 0:
        return f"{obstruction} {control}"[:OBSTRUCTION_SUMMARY_MAX_CHARS]
    return f"{obstruction[:budget]} {control}".strip()


def _runtime_obstruction_summaries(obstructions: list[PageObstruction]) -> list[str]:
    summaries: list[str] = []
    for obstruction_entry in obstructions[:_RUNTIME_SUMMARY_MAX_ITEMS]:
        entry = obstruction_entry.model_dump(mode="json", exclude_none=True)
        obstruction = _runtime_summary_entry(
            entry, _OBSTRUCTION_KEYS, _OBSTRUCTION_FIELD_MAX_CHARS, OBSTRUCTION_SUMMARY_MAX_CHARS
        )
        visible_controls = entry.get("visible_controls")
        controls = (
            [control for control in visible_controls if isinstance(control, dict)]
            if isinstance(visible_controls, list)
            else []
        )
        control_summaries = [
            summary
            for summary in (
                _runtime_summary_entry(
                    control, _OBSTRUCTION_CONTROL_KEYS, _OBSTRUCTION_FIELD_MAX_CHARS, OBSTRUCTION_SUMMARY_MAX_CHARS
                )
                for control in controls
            )
            if summary
        ]
        summaries.append(
            _joined_obstruction_summary(obstruction, "; ".join(control_summaries) or _NO_DISMISS_CONTROL_SUMMARY)
        )
    return summaries


def post_run_inspection_cleanly_matches(evidence: Any, run_id: Any) -> bool:
    return (
        isinstance(evidence, dict)
        and evidence.get("source_tool") == _INSPECT_PAGE_SOURCE_TOOL
        and evidence.get("observed_after_workflow_run") is True
        and isinstance(run_id, str)
        and bool(run_id)
        and evidence.get("workflow_run_id") == run_id
        and repair_page_evidence_is_admissible(evidence)
    )


def same_run_typed_challenge_kind(evidence: dict[str, Any] | None, run_id: str | None) -> ChallengeKind | None:
    """The classifier kind only when the packet was observed after this very run, so a stale or
    foreign packet cannot name the wall a later run hit."""
    if not post_run_inspection_cleanly_matches(evidence, run_id):
        return None
    return typed_challenge_kind(evidence)


def run_challenge_is_runtime_clearable(copilot_ctx: Any, run_id: str | None) -> bool:
    """True when this run's typed challenge is one this deployment resolved that it can clear."""
    evidence = getattr(copilot_ctx, "composition_page_evidence", None)
    if not isinstance(evidence, dict):
        return False
    # A run-matched packet is the strongest reading, but only one authoring policy mints one, and the
    # stop this releases fires from the run envelope on every policy. Requiring the packet would leave
    # the release unreachable exactly where the stop still fires, so the packet's own typed kind
    # stands in when no run-matched one exists.
    challenge_kind = same_run_typed_challenge_kind(evidence, run_id) if run_id is not None else None
    if challenge_kind is None:
        challenge_kind = typed_challenge_kind(evidence)
    if challenge_kind not in RUNTIME_SOLVABLE_CHALLENGE_KINDS:
        return False
    if getattr(copilot_ctx, "captcha_solver_available", None) is not True:
        return False
    # The gate behind the cached answer is a domain denylist, so the answer speaks only for the page
    # it was resolved against; a later page pairs with no answer and keeps its wall.
    resolved_for = getattr(copilot_ctx, "captcha_solver_available_for_url", None)
    return bool(resolved_for) and resolved_for == (evidence.get("current_url") or evidence.get("inspected_url"))


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
    has_interactive_controls = isinstance(controls, list) and bool(interactive_challenge_controls(controls))
    return has_indicators and has_interactive_controls


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
    missing_output_context = _missing_output_dependency_context(
        copilot_ctx=copilot_ctx,
        block_label=block_label,
        failed_block_status=failed_block_status or None,
        failure_reason=failure_reason,
        run_id=run_id,
    )
    if missing_output_context is not None:
        copilot_ctx.pending_code_authoring_runtime_repair_context = missing_output_context
        return
    copilot_ctx.pending_code_authoring_runtime_repair_context = CodeAuthoringRepairContext(
        block_label=block_label,
        reason_code=_RUNTIME_AUTHORING_REASON_CODE,
        runtime_failure_reason=failure_reason,
        failed_block_status=failed_block_status or None,
        workflow_run_id=run_id,
        repair_instruction=(
            "adapt the next code block to the observed page state and do not re-emit the same failing selector "
            "or name path."
        ),
    )


def _policy_allows_runtime_authoring_repair(copilot_ctx: Any) -> bool:
    return normalize_block_authoring_policy(getattr(copilot_ctx, "block_authoring_policy", None)) == (
        BlockAuthoringPolicy.CODE_ONLY_BROWSER
    )


def run_id_from_result_data(data: dict[str, Any]) -> str | None:
    run_id = data.get("workflow_run_id")
    return run_id if isinstance(run_id, str) and run_id.strip() else None


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
    return _error_text_requires_stop(copilot_ctx, data) or _error_text_requires_ask(data)


def _result_has_terminal_or_ask_precedence(copilot_ctx: Any, data: dict[str, Any], result: dict[str, Any]) -> bool:
    if _error_text_requires_stop(copilot_ctx, data, result):
        return True
    if _error_text_requires_ask(data, result):
        return True
    if data.get("skip_reason") == "workflow_credential_inputs_unbound":
        return True
    if data.get("failure_type") == "missing_credential_or_init":
        return True
    categories = data.get("failure_categories")
    if not isinstance(categories, list):
        return False
    # Only the challenge branches yield to a clearable challenge. An unreachable sandbox stops the
    # turn whatever else the page happened to be showing.
    challenge_clearable = run_challenge_is_runtime_clearable(copilot_ctx, run_id_from_result_data(data))
    for entry in categories:
        if not isinstance(entry, dict):
            continue
        category = entry.get("category")
        if category == "UNRECOVERABLE_TOOL_ERROR" and is_carrier_backed_category_entry(entry):
            return True
        if challenge_clearable:
            continue
        if category == "ANTI_BOT_DETECTION" and is_carrier_backed_category_entry(entry):
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
    if not repair_page_evidence_is_admissible(evidence):
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
    if pending.reason_code != _RUNTIME_AUTHORING_REASON_CODE:
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
    page_form_summaries = _runtime_form_summaries(evidence.get("forms"))
    page_result_summaries = _runtime_result_summaries(evidence.get("result_containers"))
    page_action_summaries = _runtime_summary_list(evidence.get("navigation_targets"), ("text", "disabled"))
    page_challenge_summaries = _runtime_summary_list(evidence.get("challenge_controls"), ("text", "disabled"))
    page_obstructions, page_obstruction_omission_notices = _typed_runtime_page_obstructions(evidence)
    page_obstruction_summaries = _runtime_obstruction_summaries(page_obstructions)
    finalized = pending.model_copy(
        update={
            "current_origin": _origin_from_runtime_url(current_url),
            "current_url": _safe_runtime_page_url(current_url),
            "current_title": _bounded_runtime_text(page_title, 160) or None,
            "page_evidence_source": _bounded_runtime_text(evidence.get("source_tool"), 80) or None,
            "observed_after_workflow_run": bool(
                page_form_summaries
                or page_result_summaries
                or page_action_summaries
                or page_challenge_summaries
                or page_obstructions
            ),
            "page_form_summaries": page_form_summaries,
            "page_result_summaries": page_result_summaries,
            "page_action_summaries": page_action_summaries,
            "page_challenge_summaries": page_challenge_summaries,
            "page_obstruction_summaries": page_obstruction_summaries,
            "page_obstructions": page_obstructions,
            "page_obstruction_omission_notices": page_obstruction_omission_notices,
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
    LOG.info(
        "Injected runtime authoring repair context",
        observed_after_workflow_run=repair_context.observed_after_workflow_run,
        workflow_run_id=repair_context.workflow_run_id,
        page_form_summary_count=len(repair_context.page_form_summaries),
        page_result_summary_count=len(repair_context.page_result_summaries),
        page_action_summary_count=len(repair_context.page_action_summaries),
        page_obstruction_summary_count=len(repair_context.page_obstruction_summaries),
        page_obstruction_count=len(repair_context.page_obstructions),
    )
    data["authoring_repair_context"] = repair_context.model_dump(mode="json")

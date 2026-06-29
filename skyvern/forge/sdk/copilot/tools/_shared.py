from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import structlog
import yaml

from skyvern.forge import app
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal, stash_blocker_signal
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
from skyvern.forge.sdk.copilot.composition_evidence import has_bounded_page_schema, parse_composition_structured
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import TOTAL_TIMEOUT_SECONDS
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.turn_halt import stash_turn_halt_from_blocker_signal
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.workflows import BlockType
from skyvern.utils.yaml_loader import safe_load_no_dates

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


# Reserve final-reply room; active block runs shrink their own budget near the deadline.
COPILOT_FINAL_REPLY_RESERVE_SECONDS = 90


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
_TASK_OUTPUT_PARAMETER_SUFFIX = "_output"


def _workflow_output_parameter_payloads(extracted_data: Any) -> dict[str, Any]:
    """Return workflow output-parameter values embedded in a block output."""
    if not isinstance(extracted_data, dict):
        return {}
    return {
        key: value
        for key, value in extracted_data.items()
        if isinstance(key, str) and key.endswith(_TASK_OUTPUT_PARAMETER_SUFFIX)
    }


def _registered_output_parameter_payloads(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """``workflow_run_output_parameters`` is the runtime's authoritative persisted
    value surface. Keep this scoped to the run result carrying it so prior-run
    accumulated outputs cannot satisfy the current run's completion contract.
    """
    run_id = data.get("workflow_run_id")
    registered_values = data.get("registered_output_parameter_values")
    registered_outputs = data.get("workflow_run_output_parameters")
    registered = []
    if isinstance(registered_values, list):
        registered.extend(registered_values)
    if isinstance(registered_outputs, list):
        registered.extend(registered_outputs)
    if not registered:
        return []
    if not isinstance(run_id, str):
        # Without a current run id the payloads can't be attributed to this run;
        # fail closed so prior-run accumulated outputs can't satisfy its contract.
        return []
    payloads: list[Mapping[str, Any]] = []
    for item in registered:
        if not isinstance(item, Mapping):
            continue
        if item.get("workflow_run_id") != run_id:
            continue
        payloads.append(item)
    return payloads


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
        payload = {field: extracted_data.get(field) for field in _TASK_OUTPUT_PAYLOAD_FIELDS}
        payload.update(_workflow_output_parameter_payloads(extracted_data))
        return payload
    return extracted_data


BLOCK_RUNNING_TOOLS = frozenset({"run_blocks_and_collect_debug", "update_and_run_blocks"})

_CONSECUTIVE_LOOP_GUARD_EXEMPT_TOOLS = BLOCK_RUNNING_TOOLS | {"fill_credential_field"}


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


def _emit_tool_blocker_signal(ctx: AgentContext, signal: CopilotToolBlockerSignal) -> str:
    payload = stash_blocker_signal(ctx, signal)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="tool_blocker_signal")
    return payload


def _block_type_name(block: object) -> str:
    """Lowercase string name of a block's type, for both YAML and runtime blocks."""
    bt = getattr(block, "block_type", None)
    if bt is None:
        return ""
    name = getattr(bt, "value", None) or getattr(bt, "name", None) or str(bt)
    return str(name).lower()


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
        if any(_block_type_name(block) in _OUTCOME_EVIDENCE_BLOCK_TYPES for block in blocks):
            return True
        code_labels = [
            getattr(block, "label", None) for block in blocks if _block_type_name(block) == BlockType.CODE.value
        ]
        return _code_artifact_metadata_covers_terminal_criterion(ctx, code_labels)
    workflow_yaml = getattr(ctx, "last_workflow_yaml", None)
    if not isinstance(workflow_yaml, str):
        return False
    parsed_blocks = [block for block in (_parse_workflow_blocks(workflow_yaml) or []) if isinstance(block, dict)]
    if any(_enum_or_string_name(block.get("block_type")) in _OUTCOME_EVIDENCE_BLOCK_TYPES for block in parsed_blocks):
        return True
    code_labels = [
        _block_label_from_yaml(block)
        for block in parsed_blocks
        if _enum_or_string_name(block.get("block_type")) == BlockType.CODE.value
    ]
    return _code_artifact_metadata_covers_terminal_criterion(ctx, code_labels)


def _code_artifact_metadata_covers_terminal_criterion(ctx: object, labels: list[str | None]) -> bool:
    metadata = getattr(ctx, "code_artifact_metadata", None)
    if not isinstance(metadata, dict):
        return False
    return any(
        isinstance(metadata.get(label), dict) and _artifact_entry_claims_terminal_criterion(metadata[label])
        for label in labels
        if isinstance(label, str)
    )


def _artifact_entry_claims_terminal_criterion(entry: dict[str, Any]) -> bool:
    criteria = entry.get("completion_criteria")
    criteria_rows = [row for row in criteria if isinstance(row, dict)] if isinstance(criteria, list) else []
    terminal_ids = {
        str(row.get("id") or "").strip()
        for row in criteria_rows
        if row.get("terminal") is True or str(row.get("level") or "").strip() == "terminal"
    } - {""}
    if not terminal_ids:
        return False
    claims = entry.get("claimed_outcomes")
    if not isinstance(claims, list):
        return False
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        covered: set[str] = set()
        for field_name in ("covered_criteria", "criteria_ids"):
            values = claim.get(field_name)
            if isinstance(values, list):
                covered.update(str(item).strip() for item in values)
        if covered & terminal_ids:
            return True
    return False


def _unverified_current_workflow_labels(ctx: object) -> list[str]:
    labels = _current_workflow_block_labels(ctx)
    verified = set(getattr(ctx, "verified_prefix_labels", []) or [])
    return [label for label in labels if label not in verified]


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


async def _fallback_page_info(ctx: AgentContext, session_id_override: str | None = None) -> tuple[str, str]:
    session_id = session_id_override or ctx.browser_session_id
    if not session_id:
        return "", ""
    try:
        browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
            session_id=session_id,
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


def _composition_evidence_page_url(evidence: dict[str, Any] | None) -> str | None:
    if not isinstance(evidence, dict):
        return None
    for key in ("current_url", "inspected_url"):
        value = evidence.get(key)
        if isinstance(value, str) and value.strip() and value != "current_page":
            return value.strip()
    return None


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


# Per-call timeout for each MCP primitive inside the discovery walker. The
# walker also checks the cumulative 60s wall clock between steps, but without
# a per-call cap a single hung navigate or get_html could block past the
# cumulative cap (cumulative is only checked between awaits).
_DISCOVERY_PER_CALL_TIMEOUT_SECONDS = 20.0


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


# Large enough for long single-turn reconnaissance: block_observation_refs are
# chosen after scouting, so this cap must stay above the citation window a turn
# can realistically compose against. Entries carry bounded parse summaries, not
# raw HTML or screenshots.
_MAX_FLOW_EVIDENCE = 64


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
    timeout_seconds: float = _DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
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
                timeout=timeout_seconds,
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

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.schema_validator import validate_and_fill_extraction_result
from skyvern.forge.sdk.copilot.challenge_evidence import (
    artifact_challenge_flag_key,
)
from skyvern.forge.sdk.copilot.reached_download_target import REGISTERED_DOWNLOAD_OUTPUT_KEYS
from skyvern.forge.sdk.copilot.run_outcome import trusted_terminal_challenge_category_name
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.schemas.workflows import BlockType

from ._shared import (
    _DATA_PRODUCING_BLOCK_TYPES,
    _block_data_payload,
    _is_meaningful_extracted_data,
    _registered_output_parameter_payloads,
    _workflow_output_parameter_payloads,
)

LOG = structlog.get_logger()


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
    "browser access barrier",
    "browser or environment port block",
    "browser port forbidden",
    "browser refused to render",
    "browser_port_forbidden",
    "browser_or_environment_port_block",
    "captcha",
    "challenge",
    "human verification",
    "port-forbidden",
    "requested port",
    "verify you are human",
)
# Multi-word anti-bot phrases only: the bare tokens ``captcha``/``challenge`` are
# excluded so business text mentioning them does not false-positive when a code-block
# value is scanned regardless of its key.
_BROAD_SINGLE_TOKEN_TERMS: frozenset[str] = frozenset({"captcha", "challenge"})
_ANTI_BOT_BLOCKER_PHRASES: tuple[str, ...] = tuple(
    term for term in _ANTI_BOT_BLOCKER_TERMS if term not in _BROAD_SINGLE_TOKEN_TERMS
)
# Strict subset of ``_STRUCTURED_BLOCKER_KEY_TERMS`` for the flag/status rules that
# scan arbitrary code-block JSON; broad terms like ``verification`` stay string-only.
_STRICT_BLOCKER_FLAG_TERMS: frozenset[str] = frozenset(
    {
        "blocker",
        "blocked",
        "captcha",
        "challenge",
        "human_verification",
    }
)
_BLOCKER_STATUS_KEYS: frozenset[str] = frozenset({"status", "state"})
_BLOCKER_SIBLING_MESSAGE_KEYS: frozenset[str] = frozenset({"reason", "message", "error", "failure_reason"})
_MAX_BLOCKER_STATUS_VALUE_LEN = 80


def _is_code_block_type(block_type: object) -> bool:
    return isinstance(block_type, str) and block_type.strip().upper() == BlockType.CODE.value.upper()


def _normalize_structured_key(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _looks_like_anti_bot_blocker(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in _ANTI_BOT_BLOCKER_TERMS)


def _looks_like_anti_bot_phrase(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _ANTI_BOT_BLOCKER_PHRASES)


def _structured_blocker_message(
    value: object,
    *,
    depth: int = 0,
    include_flag_keys: bool = False,
    key_terms: frozenset[str] = _STRUCTURED_BLOCKER_KEY_TERMS,
    declared_keys: frozenset[str] = frozenset(),
    scan_all_values_for_anti_bot: bool = False,
) -> str | None:
    if depth > 5:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = _normalize_structured_key(key)
            if normalized_key in declared_keys:
                continue
            if not isinstance(item, str) or not item.strip():
                continue
            has_blocker_key = normalized_key in _STRUCTURED_BLOCKER_MESSAGE_KEYS or any(
                term in normalized_key for term in key_terms
            )
            if (
                has_blocker_key
                or (
                    normalized_key in {"message", "error", "failure_reason", "reason"}
                    and _looks_like_anti_bot_blocker(item)
                )
                or (scan_all_values_for_anti_bot and _looks_like_anti_bot_phrase(item))
            ):
                return item.strip()[:240]
        for item in value.values():
            nested = _structured_blocker_message(
                item,
                depth=depth + 1,
                include_flag_keys=include_flag_keys,
                key_terms=key_terms,
                declared_keys=declared_keys,
                scan_all_values_for_anti_bot=scan_all_values_for_anti_bot,
            )
            if nested:
                return nested
        if include_flag_keys:
            flagged = _blocker_flag_or_status_message(value)
            if flagged:
                return flagged
    elif isinstance(value, list):
        for item in value:
            nested = _structured_blocker_message(
                item,
                depth=depth + 1,
                include_flag_keys=include_flag_keys,
                key_terms=key_terms,
                declared_keys=declared_keys,
                scan_all_values_for_anti_bot=scan_all_values_for_anti_bot,
            )
            if nested:
                return nested
    return None


def _blocker_flag_or_status_message(value: dict[str, Any]) -> str | None:
    for key, item in value.items():
        normalized_key = _normalize_structured_key(key)
        if item is True and any(term in normalized_key for term in _STRICT_BLOCKER_FLAG_TERMS):
            for sibling_key, sibling_item in value.items():
                if (
                    _normalize_structured_key(sibling_key) in _BLOCKER_SIBLING_MESSAGE_KEYS
                    and isinstance(sibling_item, str)
                    and sibling_item.strip()
                ):
                    return sibling_item.strip()[:240]
            return f"The run output flagged {normalized_key.replace('_', ' ')}."
        if (
            isinstance(item, str)
            and normalized_key in _BLOCKER_STATUS_KEYS
            and 0 < len(item.strip()) <= _MAX_BLOCKER_STATUS_VALUE_LEN
            and any(term in item.strip().lower() for term in _STRICT_BLOCKER_FLAG_TERMS)
        ):
            return f"The run output reported status '{item.strip()}'."
    return None


def _declared_code_output_keys(copilot_ctx: Any, block_label: object) -> frozenset[str]:
    """Output keys the block's code-artifact metadata declares as goal content
    (claimed-outcome ids, entities, required tokens) — the #12034 typed source.
    A declared key is never string-matched into a blocker signal."""
    metadata = getattr(copilot_ctx, "code_artifact_metadata", None) if copilot_ctx is not None else None
    if not isinstance(metadata, dict) or not isinstance(block_label, str):
        return frozenset()
    entry = metadata.get(block_label)
    if not isinstance(entry, dict):
        return frozenset()
    declared: set[str] = set()
    claims = entry.get("claimed_outcomes")
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, dict):
            continue
        for field_name in ("id", "entities", "required_tokens"):
            value = claim.get(field_name)
            values = value if isinstance(value, list) else [value]
            declared.update(
                _normalize_structured_key(item) for item in values if isinstance(item, str) and item.strip()
            )
    return frozenset(declared)


def _run_blocks_structured_blocker_message(result: dict[str, Any], copilot_ctx: Any = None) -> str | None:
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
        if block.get("status") != "completed":
            continue
        if _is_code_block_type(block_type):
            # Code-block outputs are arbitrary JSON the model authored: key matching
            # uses the strict term set (broad terms like ``verification`` belong to
            # the page-text arms) and metadata-declared goal keys are exempt. A value
            # carrying a real anti-bot phrase is still caught regardless of its key.
            blocker = _structured_blocker_message(
                block.get("extracted_data"),
                include_flag_keys=True,
                key_terms=_STRICT_BLOCKER_FLAG_TERMS,
                declared_keys=_declared_code_output_keys(copilot_ctx, block.get("label")),
                scan_all_values_for_anti_bot=True,
            )
        elif block_type in _DATA_PRODUCING_BLOCK_TYPES:
            payload = _block_data_payload(block.get("extracted_data"), block_type)
            blocker = _structured_blocker_message(payload)
        else:
            continue
        if blocker:
            return blocker
    return None


def _artifact_challenge_flag_from_result(result: dict[str, Any], copilot_ctx: Any = None) -> str | None:
    """First typed anti-bot artifact marker in the run output, or ``None``. This is
    the artifact carrier; free-text scans are not. Only block outputs and registered
    output parameters are typed payloads, so their string marker values count; the
    run envelope's own string fields are prose/status (``failure_reason`` etc.) and
    are scanned for typed boolean flags only, never marker values."""
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    flag = artifact_challenge_flag_key(
        {key: value for key, value in data.items() if key != "blocks"},
        match_marker_values=False,
    )
    if flag:
        return flag
    blocks = data.get("blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict) or block.get("status") != "completed":
                continue
            declared_keys = (
                _declared_code_output_keys(copilot_ctx, block.get("label"))
                if _is_code_block_type(block.get("block_type"))
                else frozenset()
            )
            flag = artifact_challenge_flag_key(block.get("extracted_data"), declared_keys=declared_keys)
            if flag:
                return flag
    for registered in _registered_output_parameter_payloads(data):
        flag = artifact_challenge_flag_key(registered.get("value"))
        if flag:
            return flag
    return None


def _is_blocker_term_key(key: object, declared_keys: frozenset[str] = frozenset()) -> bool:
    normalized_key = _normalize_structured_key(key)
    if normalized_key in declared_keys:
        return False
    return any(term in normalized_key for term in _STRICT_BLOCKER_FLAG_TERMS)


def _code_output_contains_collection(
    value: Any, *, depth: int = 0, declared_keys: frozenset[str] = frozenset()
) -> bool:
    if depth > 5:
        return False
    if isinstance(value, (list, tuple)):
        return True
    if isinstance(value, dict):
        return any(
            _code_output_contains_collection(item, depth=depth + 1, declared_keys=declared_keys)
            for key, item in value.items()
            if not _is_blocker_term_key(key, declared_keys)
        )
    return False


def _code_output_has_goal_content(value: Any, *, depth: int = 0, declared_keys: frozenset[str] = frozenset()) -> bool:
    """Goal content in a code block's output: a non-empty string, truthy number, or
    non-empty collection surviving after blocker-term, status, and boolean entries
    are stripped (status/state values are machine shape, not goal data)."""
    if depth > 5:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    if isinstance(value, dict):
        return any(
            _code_output_has_goal_content(item, depth=depth + 1, declared_keys=declared_keys)
            for key, item in value.items()
            if not _is_blocker_term_key(key, declared_keys)
            and _normalize_structured_key(key) not in _BLOCKER_STATUS_KEYS
        )
    return False


def _metadata_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _metadata_goal_value_paths(value: Any) -> list[str]:
    # Keep in sync with workflow_update._artifact_goal_value_paths; duplicated
    # locally to avoid importing the authoring validator into runtime blockers.
    return [path for path in _metadata_string_list(value) if not path.casefold().startswith("<fill")]


def _goal_value_paths_for_code_block(copilot_ctx: Any | None, label: Any) -> list[str]:
    if copilot_ctx is None or not isinstance(label, str):
        return []
    metadata = getattr(copilot_ctx, "code_artifact_metadata", None)
    if not isinstance(metadata, dict):
        return []
    entry = metadata.get(label)
    if not isinstance(entry, dict):
        return []

    paths: list[str] = []
    seen: set[str] = set()
    for row_group in (entry.get("claimed_outcomes"), entry.get("terminal_verifier_expectations")):
        rows = [row for row in row_group if isinstance(row, dict)] if isinstance(row_group, list) else []
        for row in rows:
            for path in _metadata_goal_value_paths(row.get("goal_value_paths")):
                if path not in seen:
                    seen.add(path)
                    paths.append(path)
    return paths


def _parse_metadata_extraction_schema(value: Any) -> dict[str, Any] | None:
    # Keep in sync with workflow_update._parse_extraction_schema; duplicated locally
    # so runtime blockers do not import the authoring validator.
    if isinstance(value, dict):
        return value or None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.casefold() in {"null", "none"} or text.casefold().startswith("<fill"):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) and parsed else None


def _extraction_schema_for_code_block(copilot_ctx: Any | None, label: Any) -> dict[str, Any] | None:
    if copilot_ctx is None or not isinstance(label, str):
        return None
    metadata = getattr(copilot_ctx, "code_artifact_metadata", None)
    if not isinstance(metadata, dict):
        return None
    entry = metadata.get(label)
    if not isinstance(entry, dict):
        return None
    # claimed_outcomes wins when both groups declare a schema; they are expected to carry the same value.
    for row_group in (entry.get("claimed_outcomes"), entry.get("terminal_verifier_expectations")):
        rows = [row for row in row_group if isinstance(row, dict)] if isinstance(row_group, list) else []
        for row in rows:
            schema = _parse_metadata_extraction_schema(row.get("extraction_schema"))
            if schema is not None:
                return schema
    return None


_GOAL_PATH_INDEX_PATTERN = re.compile(r"\[\d+\]")


def _normalize_goal_value_path(path: str) -> list[str]:
    normalized = path.strip()
    if normalized.startswith("$."):
        normalized = normalized[2:]
    elif normalized.startswith("$"):
        normalized = normalized[1:]
    normalized = normalized.replace("[*]", "[]")
    normalized = _GOAL_PATH_INDEX_PATTERN.sub("[]", normalized)
    return [part for part in normalized.split(".") if part]


def _iter_goal_value_path_values(value: Any, path_parts: list[str]) -> list[Any]:
    if not path_parts:
        return [value]
    current_part = path_parts[0]
    if isinstance(value, (list, tuple, set)):
        child_parts = path_parts[1:] if current_part == "[]" else path_parts
        expanded_values: list[Any] = []
        for item in value:
            expanded_values.extend(_iter_goal_value_path_values(item, child_parts))
        return expanded_values

    if current_part == "[]":
        return []

    expand_collection = current_part.endswith("[]")
    key = current_part[:-2] if expand_collection else current_part
    if not isinstance(value, Mapping) or key not in value:
        return []

    next_value = value.get(key)
    remaining = path_parts[1:]
    if expand_collection:
        if isinstance(next_value, (list, tuple)):
            child_values: list[Any] = []
            for item in next_value:
                child_values.extend(_iter_goal_value_path_values(item, remaining))
            return child_values
        return []
    return _iter_goal_value_path_values(next_value, remaining)


def _unmet_code_output_goal_paths(value: Any, goal_value_paths: list[str]) -> list[str]:
    """The declared goal-value paths this output retained no content for, in declared order."""
    unmet: list[str] = []
    for path in goal_value_paths:
        path_parts = _normalize_goal_value_path(path)
        values = _iter_goal_value_path_values(value, path_parts)
        if not values and _goal_value_path_targets_registered_download(path):
            values = _registered_download_output_values(value)
        if not any(_code_output_goal_path_value_has_content(item) for item in values):
            unmet.append(".".join(path_parts) or path.strip())
    return unmet


def _code_output_goal_path_value_has_content(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    return _code_output_has_goal_content(value)


def _goal_value_path_targets_registered_download(path: str) -> bool:
    normalized = path.strip()
    if normalized.startswith("$."):
        normalized = normalized[2:]
    elif normalized.startswith("$"):
        normalized = normalized[1:]
    head = normalized.split(".", 1)[0].split("[", 1)[0].strip()
    return head in REGISTERED_DOWNLOAD_OUTPUT_KEYS


def _registered_download_output_values(value: Any) -> list[Any]:
    if not isinstance(value, Mapping):
        return []
    return [value[key] for key in REGISTERED_DOWNLOAD_OUTPUT_KEYS if key in value]


def _allows_post_run_current_page_inspection_budget_bypass(ctx: AgentContext, *, use_current_page: bool) -> bool:
    if not use_current_page:
        return False
    run_id = getattr(ctx, "last_run_blocks_workflow_run_id", None)
    if not isinstance(run_id, str) or not run_id:
        return False
    if getattr(ctx, "last_test_ok", None) is None:
        return False
    return getattr(ctx, "post_run_current_page_inspection_workflow_run_id", None) != run_id


def _analyze_run_blocks(
    result: dict[str, Any], copilot_ctx: Any | None = None
) -> tuple[str | None, bool, list[dict] | None, list[dict[str, str]]]:
    """Single-pass analysis of run result blocks.

    Returns ``(anti_bot_match, has_empty_data_blocks, failure_categories,
    unmet_goal_path_omissions)`` by iterating the block list once. When
    ``data["failure_categories"]`` is already populated by a structured producer, honor it.
    Runtime prose and block source never mint a second classification plane here.
    """
    data = result.get("data")
    if not isinstance(data, dict):
        return None, False, None, []

    anti_bot_match: str | None = None

    precomputed_categories = data.get("failure_categories")
    if isinstance(precomputed_categories, list) and precomputed_categories:
        for cat in precomputed_categories:
            if isinstance(cat, dict) and trusted_terminal_challenge_category_name(cat) is not None:
                anti_bot_match = cat.get("reasoning", "anti-bot pattern detected")
                break
        return anti_bot_match, False, precomputed_categories, []

    has_data_blocks = False
    any_data_output = False
    unmet_goal_path_omissions: list[dict[str, str]] = []

    blocks = data.get("blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("block_type")
            if block.get("status") != "completed":
                continue
            if block_type in _DATA_PRODUCING_BLOCK_TYPES:
                has_data_blocks = True
                payload = _block_data_payload(block.get("extracted_data"), block_type)
                if _is_meaningful_extracted_data(payload):
                    any_data_output = True
            elif _is_code_block_type(block_type):
                extracted = block.get("extracted_data")
                goal_value_paths = _goal_value_paths_for_code_block(copilot_ctx, block.get("label"))
                if extracted is None and not goal_value_paths:
                    continue
                declared_keys = _declared_code_output_keys(copilot_ctx, block.get("label"))
                output_parameter_payloads = _workflow_output_parameter_payloads(extracted)
                if output_parameter_payloads:
                    has_data_blocks = True
                    if any(_is_meaningful_extracted_data(value) for value in output_parameter_payloads.values()):
                        any_data_output = True
                extraction_schema = _extraction_schema_for_code_block(copilot_ctx, block.get("label"))
                # Array-typed schemas would coerce the keyed dict return to [] (fill_missing_fields), making the
                # goal-path check below read a real extraction as empty; the keyed-return floor guarantees a dict.
                if (
                    extraction_schema is not None
                    and isinstance(extracted, dict)
                    and extraction_schema.get("type") != "array"
                ):
                    extracted = validate_and_fill_extraction_result(extracted, extraction_schema)
                if goal_value_paths:
                    has_data_blocks = True
                    unmet_paths = _unmet_code_output_goal_paths(extracted, goal_value_paths)
                    if _is_meaningful_extracted_data(extracted):
                        any_data_output = True
                    if unmet_paths:
                        label = block.get("label")
                        unmet_goal_path_omissions.extend(
                            {"block_label": label if isinstance(label, str) else "", "output_path": path}
                            for path in unmet_paths
                        )
                    continue
                # A code output joins the emptiness denominator only when it declares a
                # collection shape; action-only outputs are exempt.
                if _code_output_contains_collection(extracted, declared_keys=declared_keys):
                    has_data_blocks = True
                if _code_output_has_goal_content(extracted, declared_keys=declared_keys):
                    any_data_output = True

    top_level_output_payloads = _workflow_output_parameter_payloads(data.get("output"))
    if top_level_output_payloads:
        has_data_blocks = True
        if any(_is_meaningful_extracted_data(value) for value in top_level_output_payloads.values()):
            any_data_output = True

    registered_payloads = _registered_output_parameter_payloads(data)
    if registered_payloads:
        has_data_blocks = True
        for registered in registered_payloads:
            if _is_meaningful_extracted_data(registered.get("value")):
                any_data_output = True
    # Declared-path omissions are observations for the acting model, not a
    # deterministic judgment that an otherwise completed run needs repair.
    empty_data_blocks = has_data_blocks and not any_data_output
    return anti_bot_match, empty_data_blocks, None, unmet_goal_path_omissions

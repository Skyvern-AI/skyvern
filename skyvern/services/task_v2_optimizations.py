"""Independently ablatable, run-sticky Task V2 token optimizations."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext

LOG = structlog.get_logger()

TASK_V2_FLAG_PREFIX = "TASK_V2_"
DEFAULT_GENERATED_LOOP_ITEM_LIMIT = 25
HARD_GENERATED_LOOP_ITEM_LIMIT = 50
DEFAULT_COMPLETION_CHECK_INTERVAL = 3
COMPACT_HISTORY_RECORD_LIMIT = 4
COMPACT_HISTORY_FIELD_MAX_CHARS = 2000


@dataclass(frozen=True)
class TaskV2OptimizationFlags:
    """The feature-flag cohort pinned to one Task V2 run."""

    honor_iteration_override: bool = False
    no_progress_breaker: bool = False
    completion_scheduler: bool = False
    observation_reuse: bool = False
    compact_context: bool = False
    loop_contract: bool = False
    loop_guardrails: bool = False
    loop_fail_fast: bool = False
    planning_deduplication: bool = False
    extraction_schema_reuse: bool = False
    loop_replay: bool = False

    def as_mapping(self) -> dict[str, bool]:
        return asdict(self)


FLAG_NAME_BY_FIELD = {
    field_name: f"{TASK_V2_FLAG_PREFIX}{field_name.upper()}"
    for field_name in TaskV2OptimizationFlags.__dataclass_fields__
}


async def resolve_task_v2_optimization_flags(context: SkyvernContext) -> TaskV2OptimizationFlags:
    """Resolve every Task V2 optimization once and pin the result to ``context``."""

    if context.task_v2_optimization_flags is not None:
        return TaskV2OptimizationFlags(**context.task_v2_optimization_flags)

    distinct_id = context.workflow_run_id or context.task_v2_id or context.run_id
    provider = getattr(app, "EXPERIMENTATION_PROVIDER", None)
    if not distinct_id or provider is None:
        flags = TaskV2OptimizationFlags()
        context.task_v2_optimization_flags = flags.as_mapping()
        return flags

    properties = {
        "organization_id": context.organization_id,
        "workflow_permanent_id": context.workflow_permanent_id,
    }

    async def resolve(field_name: str) -> tuple[str, bool]:
        try:
            enabled = await provider.is_feature_enabled_cached(
                FLAG_NAME_BY_FIELD[field_name], distinct_id, properties=properties
            )
        except Exception:
            LOG.warning(
                "Failed to resolve Task V2 optimization flag; using control",
                flag_name=FLAG_NAME_BY_FIELD[field_name],
                task_v2_id=context.task_v2_id,
                exc_info=True,
            )
            enabled = False
        return field_name, bool(enabled)

    resolved = dict(await asyncio.gather(*(resolve(name) for name in FLAG_NAME_BY_FIELD)))
    flags = TaskV2OptimizationFlags(**resolved)
    context.task_v2_optimization_flags = flags.as_mapping()
    return flags


def normalize_loop_values(
    values: Any,
    *,
    requested_limit: int | None,
    apply_guardrail: bool,
) -> list[str]:
    """Validate, deduplicate, and bound values generated for a Task V2 loop."""

    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)

    try:
        parsed_limit = int(requested_limit) if requested_limit is not None else None
    except (TypeError, ValueError):
        parsed_limit = None
    limit = parsed_limit if parsed_limit and parsed_limit > 0 else None
    if apply_guardrail:
        limit = min(limit or DEFAULT_GENERATED_LOOP_ITEM_LIMIT, HARD_GENERATED_LOOP_ITEM_LIMIT)
    if limit is not None:
        normalized = normalized[:limit]
    return normalized


def generated_loop_item_limit(requested_limit: Any) -> int:
    """Resolve the configurable soft limit without ever exceeding the hard cap."""

    try:
        parsed_limit = int(requested_limit) if requested_limit is not None else None
    except (TypeError, ValueError):
        parsed_limit = None
    return min(parsed_limit or DEFAULT_GENERATED_LOOP_ITEM_LIMIT, HARD_GENERATED_LOOP_ITEM_LIMIT)


def compact_task_history(task_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render a bounded copy of recent history while preserving the stored history."""

    compacted = copy.deepcopy(task_history[-COMPACT_HISTORY_RECORD_LIMIT:])
    for record in compacted:
        for key, value in list(record.items()):
            serialized = json.dumps(value, default=str) if not isinstance(value, str) else value
            if len(serialized) > COMPACT_HISTORY_FIELD_MAX_CHARS:
                record[key] = serialized[:COMPACT_HISTORY_FIELD_MAX_CHARS] + "...[truncated]"
    return compacted


def observation_fingerprint(url: str | None, scraped_page: Any, plan: str, task_type: str) -> str:
    """Build a stable fingerprint for detecting repeated plans on unchanged pages."""

    element_tree = getattr(scraped_page, "element_tree_trimmed", None)
    payload = json.dumps(
        {"url": url, "elements": element_tree, "plan": plan, "task_type": task_type},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def should_run_completion_check(
    *,
    iteration: int,
    task_type: str,
    has_extracted_data: bool,
    interval: int = DEFAULT_COMPLETION_CHECK_INTERVAL,
) -> bool:
    """Return whether the reduced-frequency completion gate is due."""

    return iteration == 0 or task_type == "extract" or has_extracted_data or (iteration + 1) % interval == 0

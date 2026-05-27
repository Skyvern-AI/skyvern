"""Cohort resolution for the v3 experiment.

Two flags:

* ``SCRIPT_REVIEWER_VERSION`` — multivariate string: ``"v2"`` (default) or
  ``"v3"``. Distinct_id = ``workflow_permanent_id`` (per-workflow bucketing,
  not per-run — matches the existing ``EXTRACT_INFORMATION_CACHE_REDIS``
  pattern documented in ``.claude/rules/backend.md``). Properties carry
  ``organization_id`` and ``workflow_run_id`` for targeting and logging.

* ``SCRIPT_REVIEWER_V3_BUDGET`` — payload flag returning the tunable
  budget JSON. Missing / malformed payload logs a warning and falls back to
  hardcoded defaults in ``budget.py``.

Both flags are read via ``app.EXPERIMENTATION_PROVIDER`` — skyvern/ never
imports cloud/ (OSS boundary enforced by pre-commit hook).

**Local-test escape hatch**: the env var ``SKYVERN_V3_REVIEWER_FORCE``
short-circuits provider resolution. Values ``"v3"`` or ``"true"`` force v3,
``"v2"`` or ``"false"`` force v2. Empty / unset falls through to the provider.
This is intended for local benchmark runs that don't have an experimentation
allowlist for the test wpid — production never sets the env var.

**PR 2 scope note**: this module defines the resolution helpers. Call sites
(hook in ``SkyvernPage`` methods) are wired in PR 3.
"""

from __future__ import annotations

import os

import structlog

from skyvern.forge import app
from skyvern.services.script_reviewer_v3.budget import (
    DEFAULT_MIDRUN_MAX_COST_PER_RUN_USD,
    DEFAULT_MIDRUN_MAX_COST_USD,
    DEFAULT_MIDRUN_MAX_CYCLES,
    DEFAULT_MIDRUN_MAX_INVOCATIONS_PER_RUN,
    DEFAULT_MIDRUN_MAX_TOKENS,
    DEFAULT_MIDRUN_MAX_WALL_SECONDS,
    DEFAULT_POSTRUN_MAX_COST_USD,
    DEFAULT_POSTRUN_MAX_CYCLES,
    DEFAULT_POSTRUN_MAX_TOKENS,
    DEFAULT_POSTRUN_MAX_WALL_SECONDS,
    RunBudget,
)

LOG = structlog.get_logger()


SCRIPT_REVIEWER_VERSION_FLAG = "SCRIPT_REVIEWER_VERSION"
SCRIPT_REVIEWER_V3_BUDGET_FLAG = "SCRIPT_REVIEWER_V3_BUDGET"

# Canonical variant values. Anything else is treated as the default ("v2").
VARIANT_V2 = "v2"
VARIANT_V3 = "v3"


async def is_v3_cohort(
    workflow_permanent_id: str | None,
    organization_id: str | None = None,
    workflow_run_id: str | None = None,
) -> bool:
    """Return True when the SCRIPT_REVIEWER_VERSION flag resolves to "v3" for
    this wpid. Returns False on any other variant, missing wpid, or failure.

    Per-wpid bucketing means a given workflow is consistently routed to one
    reviewer version across all its runs (see task_plan.md Non-Goals).

    Emits a ``script_reviewer_version_resolved`` structured log at INFO so the
    cohort distribution is observable from day 0. Subsequent calls with the
    same wpid use the provider's cache (via ``get_value_cached``).
    """
    forced = (os.environ.get("SKYVERN_V3_REVIEWER_FORCE") or "").strip().lower()
    if forced in {"v3", "true", "1", "yes"}:
        LOG.info(
            "script_reviewer_version_resolved",
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            variant=VARIANT_V3,
            raw_variant=None,
            source="env_override",
        )
        return True
    if forced in {"v2", "false", "0", "no"}:
        LOG.info(
            "script_reviewer_version_resolved",
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            variant=VARIANT_V2,
            raw_variant=None,
            source="env_override",
        )
        return False

    if not workflow_permanent_id:
        # Defensive: without a wpid we can't bucket. Fail closed to v2.
        return False
    if not app.EXPERIMENTATION_PROVIDER:
        return False

    variant: str | None = None
    try:
        variant = await app.EXPERIMENTATION_PROVIDER.get_value_cached(
            SCRIPT_REVIEWER_VERSION_FLAG,
            workflow_permanent_id,
            properties={
                "organization_id": organization_id or "",
                "workflow_run_id": workflow_run_id or "",
            },
        )
    except Exception:
        LOG.warning(
            "Failed to resolve SCRIPT_REVIEWER_VERSION flag; defaulting to v2",
            workflow_permanent_id=workflow_permanent_id,
            exc_info=True,
        )
        return False

    resolved = variant if variant in (VARIANT_V2, VARIANT_V3) else VARIANT_V2
    LOG.info(
        "script_reviewer_version_resolved",
        workflow_permanent_id=workflow_permanent_id,
        organization_id=organization_id,
        workflow_run_id=workflow_run_id,
        variant=resolved,
        raw_variant=variant,
    )
    return resolved == VARIANT_V3


async def resolve_v3_budget_payload(
    workflow_permanent_id: str | None,
    organization_id: str | None = None,
) -> dict[str, float | int]:
    """Resolve the SCRIPT_REVIEWER_V3_BUDGET payload flag, returning a dict
    with all expected keys. Missing keys fall back to hardcoded defaults in
    ``budget.py``; malformed payload logs a warning and returns all defaults.

    Distinct_id matches the version flag (workflow_permanent_id) so tuning
    can be per-workflow.
    """
    defaults: dict[str, float | int] = {
        "midrun_max_cycles": DEFAULT_MIDRUN_MAX_CYCLES,
        "midrun_max_tokens": DEFAULT_MIDRUN_MAX_TOKENS,
        "midrun_max_cost_usd": DEFAULT_MIDRUN_MAX_COST_USD,
        "midrun_max_invocations_per_run": DEFAULT_MIDRUN_MAX_INVOCATIONS_PER_RUN,
        "midrun_max_cost_per_run_usd": DEFAULT_MIDRUN_MAX_COST_PER_RUN_USD,
        "midrun_max_wall_seconds": DEFAULT_MIDRUN_MAX_WALL_SECONDS,
        "postrun_max_cycles": DEFAULT_POSTRUN_MAX_CYCLES,
        "postrun_max_tokens": DEFAULT_POSTRUN_MAX_TOKENS,
        "postrun_max_cost_usd": DEFAULT_POSTRUN_MAX_COST_USD,
        "postrun_max_wall_seconds": DEFAULT_POSTRUN_MAX_WALL_SECONDS,
    }
    if not workflow_permanent_id or not app.EXPERIMENTATION_PROVIDER:
        return defaults

    try:
        payload = await app.EXPERIMENTATION_PROVIDER.get_payload_cached(
            SCRIPT_REVIEWER_V3_BUDGET_FLAG,
            workflow_permanent_id,
            properties={"organization_id": organization_id or ""},
        )
    except Exception:
        LOG.warning(
            "Failed to fetch SCRIPT_REVIEWER_V3_BUDGET payload; using defaults",
            workflow_permanent_id=workflow_permanent_id,
            exc_info=True,
        )
        return defaults

    if not isinstance(payload, dict):
        if payload is not None:
            LOG.warning(
                "SCRIPT_REVIEWER_V3_BUDGET payload is not a dict; using defaults",
                workflow_permanent_id=workflow_permanent_id,
                payload_type=type(payload).__name__,
            )
        return defaults

    merged = dict(defaults)
    for key, default_value in defaults.items():
        if key in payload:
            value = payload[key]
            # Permissive type coercion: provider payloads come back as JSON types.
            try:
                if isinstance(default_value, int):
                    merged[key] = int(value)
                elif isinstance(default_value, float):
                    merged[key] = float(value)
            except (TypeError, ValueError):
                LOG.warning(
                    "SCRIPT_REVIEWER_V3_BUDGET payload key has wrong type; using default",
                    workflow_permanent_id=workflow_permanent_id,
                    key=key,
                    raw_value=value,
                    raw_type=type(value).__name__,
                )
                # fall back to default by not overwriting
    return merged


async def build_run_budget(
    workflow_permanent_id: str | None,
    organization_id: str | None = None,
) -> RunBudget:
    """Construct a :class:`RunBudget` from the resolved budget payload.

    Called when a v3-cohort workflow run starts — the result lives on
    :attr:`SkyvernContext.v3_run_budget` for the duration of the run.
    """
    payload = await resolve_v3_budget_payload(workflow_permanent_id, organization_id)
    return RunBudget(
        max_invocations_per_run=int(payload["midrun_max_invocations_per_run"]),
        max_cost_per_run_usd=float(payload["midrun_max_cost_per_run_usd"]),
        per_review_cost_ceiling_usd=float(payload["midrun_max_cost_usd"]),
    )


__all__ = [
    "build_run_budget",
    "is_v3_cohort",
    "resolve_v3_budget_payload",
    "SCRIPT_REVIEWER_V3_BUDGET_FLAG",
    "SCRIPT_REVIEWER_VERSION_FLAG",
    "VARIANT_V2",
    "VARIANT_V3",
]

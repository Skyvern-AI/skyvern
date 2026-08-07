import structlog

from skyvern.config import settings
from skyvern.forge import app

LOG = structlog.get_logger()
# Tracks TASK_V2_CONVERGE_PCT in infra/k8s/temporal-worker/values-benchmark-hetzner.yaml; the flag
# carries no value of its own, so re-tuning the fleet means re-tuning this too.
DEFAULT_CONVERGE_PCT = 20


async def is_planner_levers_enabled(organization_id: str | None) -> bool:
    if not organization_id:
        return False
    try:
        return bool(
            await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                "TASK_V2_PLANNER_LEVERS",
                organization_id,
                properties={"organization_id": organization_id},
            )
        )
    except Exception:
        LOG.warning("Failed to resolve TASK_V2_PLANNER_LEVERS feature flag", exc_info=True)
        return False


async def skip_completion_check_after_navigate(organization_id: str | None) -> bool:
    return settings.TASK_V2_SKIP_COMPLETION_CHECK_AFTER_NAVIGATE or await is_planner_levers_enabled(organization_id)


async def carry_subgoals(organization_id: str | None) -> bool:
    return settings.TASK_V2_CARRY_SUBGOALS or await is_planner_levers_enabled(organization_id)


async def reset_browser_tabs_between_loop_iterations(organization_id: str | None) -> bool:
    return settings.RESET_BROWSER_TABS_BETWEEN_LOOP_ITERATIONS or await is_planner_levers_enabled(organization_id)


async def converge_pct(organization_id: str | None) -> int:
    if settings.TASK_V2_CONVERGE_PCT:
        return settings.TASK_V2_CONVERGE_PCT
    return DEFAULT_CONVERGE_PCT if await is_planner_levers_enabled(organization_id) else 0

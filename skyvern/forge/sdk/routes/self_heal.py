import structlog
from fastapi import Depends, Query
from pydantic import BaseModel

from skyvern.forge import app
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.self_heal import HealEpisodeView, HealStatus, RunHealSummary, summarize_run_heals

LOG = structlog.get_logger()


class RunHealEpisodesResponse(BaseModel):
    episodes: list[HealEpisodeView]
    summary: RunHealSummary


@base_router.get(
    "/workflows/{workflow_permanent_id}/heal_episodes",
    response_model=list[HealEpisodeView],
    include_in_schema=False,
)
async def get_workflow_heal_episodes(
    workflow_permanent_id: str,
    block_label: str | None = None,
    status: HealStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> list[HealEpisodeView]:
    episodes = await app.DATABASE.self_heal.get_heal_episodes_for_workflow(
        organization_id=organization.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        block_label=block_label,
        status=status,
        limit=limit,
        offset=offset,
    )
    return [HealEpisodeView.from_episode(episode) for episode in episodes]


@base_router.get(
    "/runs/{workflow_run_id}/heal_episodes",
    response_model=RunHealEpisodesResponse,
    include_in_schema=False,
)
async def get_run_heal_episodes(
    workflow_run_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> RunHealEpisodesResponse:
    episodes = await app.DATABASE.self_heal.get_heal_episodes_for_run(
        organization_id=organization.organization_id,
        workflow_run_id=workflow_run_id,
    )
    return RunHealEpisodesResponse(
        episodes=[HealEpisodeView.from_episode(episode) for episode in episodes],
        summary=summarize_run_heals(episodes),
    )

from __future__ import annotations

from skyvern.forge import app
from skyvern.schemas.self_heal import (
    RELIABILITY_WINDOW,
    HealEpisode,
    RunHealGroup,
    WorkflowReliability,
    compute_workflow_reliability,
)


async def get_workflow_reliability(organization_id: str, workflow_permanent_id: str) -> WorkflowReliability:
    run_ids = await app.DATABASE.self_heal.get_recent_terminal_workflow_run_ids(
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        limit=RELIABILITY_WINDOW,
    )
    episodes = await app.DATABASE.self_heal.get_heal_episodes_for_runs(
        organization_id=organization_id,
        workflow_run_ids=run_ids,
    )

    episodes_by_run_id: dict[str, list[HealEpisode]] = {run_id: [] for run_id in run_ids}
    for episode in episodes:
        if episode.workflow_run_id in episodes_by_run_id:
            episodes_by_run_id[episode.workflow_run_id].append(episode)

    groups = [RunHealGroup(workflow_run_id=run_id, episodes=episodes_by_run_id[run_id]) for run_id in run_ids]
    return compute_workflow_reliability(groups)

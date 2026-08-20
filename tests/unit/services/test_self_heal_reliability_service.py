from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.schemas.self_heal import (
    HealEpisode,
    HealStatus,
    ReliabilityState,
    WorkflowReliability,
    compute_workflow_reliability,
)
from skyvern.services import self_heal_reliability_service

ORG_ID = "org_self_heal"


def _episode(
    *,
    heal_episode_id: str,
    workflow_run_block_id: str,
    block_label: str,
    status: HealStatus,
    workflow_permanent_id: str,
    workflow_run_id: str,
) -> HealEpisode:
    now = datetime(2026, 1, 1, 0, 0, 0)
    return HealEpisode(
        heal_episode_id=heal_episode_id,
        organization_id=ORG_ID,
        workflow_permanent_id=workflow_permanent_id,
        workflow_id=f"w_{workflow_permanent_id}",
        workflow_run_id=workflow_run_id,
        workflow_run_block_id=workflow_run_block_id,
        block_label=block_label,
        engine="code",
        status=status,
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
async def test_get_workflows_reliability_batch_matches_single_with_ordering_and_unseen(monkeypatch) -> None:
    wpid_ordered = "wpid_ordered"
    wpid_other = "wpid_other"
    wpid_unseen = "wpid_unseen"

    ordered_run_ids = [f"wr_ordered_{index:02d}" for index in range(20)]
    other_run_ids = [f"wr_other_{index:02d}" for index in range(12)]

    run_ids_by_wpid = {
        wpid_ordered: ordered_run_ids,
        wpid_other: other_run_ids,
        wpid_unseen: [],
    }

    episodes = [
        _episode(
            heal_episode_id="he_ordered_0",
            workflow_run_block_id="wrb_ordered_0",
            block_label="block_ordered",
            status=HealStatus.fired_completed,
            workflow_permanent_id=wpid_ordered,
            workflow_run_id=ordered_run_ids[0],
        ),
        _episode(
            heal_episode_id="he_ordered_1",
            workflow_run_block_id="wrb_ordered_1",
            block_label="block_ordered",
            status=HealStatus.fired_completed,
            workflow_permanent_id=wpid_ordered,
            workflow_run_id=ordered_run_ids[1],
        ),
        _episode(
            heal_episode_id="he_ordered_2",
            workflow_run_block_id="wrb_ordered_2",
            block_label="block_ordered",
            status=HealStatus.fired_completed,
            workflow_permanent_id=wpid_ordered,
            workflow_run_id=ordered_run_ids[2],
        ),
        _episode(
            heal_episode_id="he_other_1",
            workflow_run_block_id="wrb_other_1",
            block_label="block_other",
            status=HealStatus.fired_failed,
            workflow_permanent_id=wpid_other,
            workflow_run_id=other_run_ids[1],
        ),
        _episode(
            heal_episode_id="he_other_7",
            workflow_run_block_id="wrb_other_7",
            block_label="block_other",
            status=HealStatus.fired_unverified,
            workflow_permanent_id=wpid_other,
            workflow_run_id=other_run_ids[7],
        ),
    ]

    get_recent_terminal_workflow_run_ids = AsyncMock(
        side_effect=lambda organization_id, workflow_permanent_id, limit: run_ids_by_wpid[workflow_permanent_id][:limit]
    )
    get_recent_terminal_workflow_run_ids_batch = AsyncMock(
        side_effect=lambda organization_id, workflow_permanent_ids, limit: {
            workflow_permanent_id: run_ids_by_wpid.get(workflow_permanent_id, [])[:limit]
            for workflow_permanent_id in workflow_permanent_ids
        }
    )
    get_heal_episodes_for_runs = AsyncMock(
        side_effect=lambda organization_id, workflow_run_ids: [
            episode for episode in episodes if episode.workflow_run_id in set(workflow_run_ids)
        ]
    )

    monkeypatch.setattr(
        self_heal_reliability_service.app.DATABASE,
        "self_heal",
        SimpleNamespace(
            get_recent_terminal_workflow_run_ids=get_recent_terminal_workflow_run_ids,
            get_recent_terminal_workflow_run_ids_batch=get_recent_terminal_workflow_run_ids_batch,
            get_heal_episodes_for_runs=get_heal_episodes_for_runs,
        ),
    )

    batch = await self_heal_reliability_service.get_workflows_reliability(
        ORG_ID,
        [wpid_ordered, wpid_other, wpid_unseen],
    )
    single_ordered = await self_heal_reliability_service.get_workflow_reliability(ORG_ID, wpid_ordered)
    single_other = await self_heal_reliability_service.get_workflow_reliability(ORG_ID, wpid_other)

    assert batch[wpid_ordered] == single_ordered
    assert batch[wpid_other] == single_other

    assert batch[wpid_ordered].consecutive_healed_runs == 3
    assert batch[wpid_ordered].state == ReliabilityState.action_needed

    assert batch[wpid_unseen] == compute_workflow_reliability([])
    assert batch[wpid_unseen] == WorkflowReliability(
        state=ReliabilityState.healthy,
        outcome_risk=False,
        scored=False,
        window_runs=0,
        healed_runs=0,
        heal_rate=0.0,
        consecutive_healed_runs=0,
        floor_runs=0,
        outcome_risk_runs=0,
    )


@pytest.mark.asyncio
async def test_get_workflows_reliability_empty_request_returns_empty_without_db_calls(monkeypatch) -> None:
    get_recent_terminal_workflow_run_ids = AsyncMock()
    get_recent_terminal_workflow_run_ids_batch = AsyncMock()
    get_heal_episodes_for_runs = AsyncMock()

    monkeypatch.setattr(
        self_heal_reliability_service.app.DATABASE,
        "self_heal",
        SimpleNamespace(
            get_recent_terminal_workflow_run_ids=get_recent_terminal_workflow_run_ids,
            get_recent_terminal_workflow_run_ids_batch=get_recent_terminal_workflow_run_ids_batch,
            get_heal_episodes_for_runs=get_heal_episodes_for_runs,
        ),
    )

    assert await self_heal_reliability_service.get_workflows_reliability(ORG_ID, []) == {}
    get_recent_terminal_workflow_run_ids.assert_not_awaited()
    get_recent_terminal_workflow_run_ids_batch.assert_not_awaited()
    get_heal_episodes_for_runs.assert_not_awaited()

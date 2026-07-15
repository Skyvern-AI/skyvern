from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import skyvern.forge.sdk.routes.self_heal as self_heal_routes
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.routes import routers as routers_module
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.self_heal import (
    HealEpisode,
    HealEpisodeView,
    HealStatus,
    OutputObligation,
    ReliabilityState,
    WorkflowReliability,
)

ORG_ID = "org_self_heal"


def _episode(
    *,
    heal_episode_id: str,
    workflow_run_block_id: str,
    block_label: str,
    status: HealStatus,
    workflow_permanent_id: str = "wpid_1",
    workflow_run_id: str = "wr_1",
    output_obligation: OutputObligation | None = None,
) -> HealEpisode:
    now = datetime(2026, 1, 1, 0, 0, 0)
    return HealEpisode(
        heal_episode_id=heal_episode_id,
        organization_id=ORG_ID,
        workflow_permanent_id=workflow_permanent_id,
        workflow_id="w_1",
        workflow_run_id=workflow_run_id,
        workflow_run_block_id=workflow_run_block_id,
        block_label=block_label,
        engine="code",
        status=status,
        output_obligation=output_obligation,
        created_at=now,
        modified_at=now,
    )


def _build_client(monkeypatch) -> tuple[TestClient, AsyncMock, AsyncMock, AsyncMock]:
    async def _fake_org() -> SimpleNamespace:
        return SimpleNamespace(organization_id=ORG_ID)

    get_for_workflow = AsyncMock()
    get_for_run = AsyncMock()
    get_reliability = AsyncMock()
    monkeypatch.setattr(forge_app.DATABASE.self_heal, "get_heal_episodes_for_workflow", get_for_workflow)
    monkeypatch.setattr(forge_app.DATABASE.self_heal, "get_heal_episodes_for_run", get_for_run)
    monkeypatch.setattr(self_heal_routes, "get_workflow_reliability", get_reliability)

    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = _fake_org
    fastapi_app.include_router(routers_module.base_router, prefix="/v1")
    return TestClient(fastapi_app), get_for_workflow, get_for_run, get_reliability


def test_get_workflow_heal_episodes_filters_to_caller_org_and_view_shape(monkeypatch) -> None:
    client, get_for_workflow, _, _ = _build_client(monkeypatch)

    async def _repo_call(**kwargs):
        assert kwargs["organization_id"] == ORG_ID
        assert kwargs["workflow_permanent_id"] == "wpid_target"
        return [
            _episode(
                heal_episode_id="he_2",
                workflow_run_block_id="wrb_2",
                block_label="block_b",
                status=HealStatus.fired_completed,
                workflow_permanent_id="wpid_target",
            ),
            _episode(
                heal_episode_id="he_1",
                workflow_run_block_id="wrb_1",
                block_label="block_a",
                status=HealStatus.fired_failed,
                workflow_permanent_id="wpid_target",
            ),
        ]

    get_for_workflow.side_effect = _repo_call

    response = client.get("/v1/workflows/wpid_target/heal_episodes")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert {item["workflow_run_block_id"] for item in body} == {"wrb_1", "wrb_2"}

    expected_keys = set(HealEpisodeView.model_fields.keys())
    for item in body:
        assert set(item.keys()) == expected_keys
        assert "block_code" not in item
        assert "block_prompt" not in item
        assert "failure_message" not in item


def test_get_workflow_heal_episodes_invalid_status_returns_422(monkeypatch) -> None:
    client, get_for_workflow, _, _ = _build_client(monkeypatch)

    response = client.get("/v1/workflows/wpid_target/heal_episodes", params={"status": "invalid_status"})

    assert response.status_code == 422
    get_for_workflow.assert_not_awaited()


def test_get_run_heal_episodes_returns_episodes_and_summary(monkeypatch) -> None:
    client, _, get_for_run, _ = _build_client(monkeypatch)
    get_for_run.return_value = [
        _episode(
            heal_episode_id="he_1",
            workflow_run_block_id="wrb_a",
            block_label="block_a",
            status=HealStatus.fired_completed,
            workflow_run_id="wr_target",
        ),
        _episode(
            heal_episode_id="he_2",
            workflow_run_block_id="wrb_b",
            block_label="block_b",
            status=HealStatus.fired_failed,
            workflow_run_id="wr_target",
            output_obligation=OutputObligation.observed,
        ),
    ]

    response = client.get("/v1/runs/wr_target/heal_episodes")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"episodes", "summary"}
    assert len(body["episodes"]) == 2
    assert set(body["summary"].keys()) == {
        "blocks_healed",
        "blocks_outcome_risk",
        "blocks_with_heal_attempt",
    }
    assert body["summary"]["blocks_healed"] == 1
    assert body["summary"]["blocks_with_heal_attempt"] == 2
    assert body["summary"]["blocks_outcome_risk"] == ["block_b"]


def test_get_workflow_reliability_is_org_scoped(monkeypatch) -> None:
    client, _, _, get_reliability = _build_client(monkeypatch)
    get_reliability.return_value = WorkflowReliability(
        state=ReliabilityState.watch,
        outcome_risk=True,
        scored=True,
        window_runs=20,
        healed_runs=2,
        heal_rate=0.1,
        consecutive_healed_runs=1,
        floor_runs=0,
        outcome_risk_runs=1,
    )

    response = client.get("/v1/workflows/wpid_target/reliability")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "state": "watch",
        "outcome_risk": True,
        "scored": True,
        "window_runs": 20,
        "healed_runs": 2,
        "heal_rate": 0.1,
        "consecutive_healed_runs": 1,
        "floor_runs": 0,
        "outcome_risk_runs": 1,
    }
    get_reliability.assert_awaited_once_with(ORG_ID, "wpid_target")

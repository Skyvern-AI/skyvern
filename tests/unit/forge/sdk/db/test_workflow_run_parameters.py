"""Tests for WorkflowRunsRepository.get_workflow_run_parameters against in-memory SQLite.

The method resolves each run parameter's definition. It used to issue one query per
parameter (sequential N+1); it now batch-fetches all definitions in a single query.
The load-bearing invariant is that a run referencing a soft-deleted parameter definition
still resolves — batch resolution must match on id only, without a deleted_at filter.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition


def _wp(key: str, workflow_id: str, default_value: Any = None) -> WorkflowParameter:
    now = datetime.now(timezone.utc)
    return WorkflowParameter(
        workflow_parameter_id=f"wp_{key}",
        workflow_parameter_type=WorkflowParameterType.STRING,
        key=key,
        description=None,
        workflow_id=workflow_id,
        default_value=default_value,
        created_at=now,
        modified_at=now,
    )


async def _seed(agent_db: AgentDB) -> dict[str, str]:
    org = await agent_db.organizations.create_organization(organization_name="Test Org", domain="wrp.test")
    workflow = await agent_db.workflows.create_workflow(
        title="Test Workflow",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org.organization_id,
    )
    return {"organization_id": org.organization_id, "workflow_id": workflow.workflow_id}


async def _reconcile(agent_db: AgentDB, ids: dict[str, str], params: list[WorkflowParameter]) -> None:
    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=ids["workflow_id"],
        organization_id=ids["organization_id"],
        workflow_definition=WorkflowDefinition(parameters=params, blocks=[]),
    )


@pytest.mark.asyncio
async def test_resolves_all_params_including_soft_deleted(agent_db: AgentDB) -> None:
    ids = await _seed(agent_db)
    workflow_id = ids["workflow_id"]

    await _reconcile(agent_db, ids, [_wp("live", workflow_id, "a"), _wp("gone", workflow_id, "b")])
    persisted = {p.key: p for p in await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id)}

    # Soft-delete "gone" — its definition row survives and must still resolve by id.
    await _reconcile(agent_db, ids, [_wp("live", workflow_id, "a")])

    workflow_run = await agent_db.workflow_runs.create_workflow_run(
        workflow_permanent_id="wpid_test",
        workflow_id=workflow_id,
        organization_id=ids["organization_id"],
    )
    await agent_db.workflow_runs.create_workflow_run_parameters(
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_parameter_values=[(persisted["live"], "live_val"), (persisted["gone"], "gone_val")],
    )

    results = await agent_db.workflow_runs.get_workflow_run_parameters(workflow_run.workflow_run_id)

    by_key = {wp.key: (wp, wrp) for wp, wrp in results}
    assert set(by_key) == {"live", "gone"}
    assert by_key["live"][1].value == "live_val"
    assert by_key["gone"][1].value == "gone_val"


@pytest.mark.asyncio
async def test_returns_empty_when_no_run_parameters(agent_db: AgentDB) -> None:
    ids = await _seed(agent_db)
    workflow_run = await agent_db.workflow_runs.create_workflow_run(
        workflow_permanent_id="wpid_test",
        workflow_id=ids["workflow_id"],
        organization_id=ids["organization_id"],
    )
    assert await agent_db.workflow_runs.get_workflow_run_parameters(workflow_run.workflow_run_id) == []

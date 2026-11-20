"""Regression tests for the shape of workflows-repository queries.

SKY-8795: workflow list search must match workflow_permanent_id.

The /workflows page sends `search_key` to the backend. Before the fix, the search
only matched `workflows.title`, `folders.title`, and parameter metadata — typing a
`wpid_*` into the search box returned no results.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event, insert

from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import WorkflowModel, WorkflowTemplateModel
from skyvern.forge.sdk.db.repositories.workflows import WorkflowsRepository
from skyvern.forge.sdk.workflow.models.workflow import Workflow


class _SessionContext:
    def __init__(self, session: MagicMock) -> None:
        self._session = session

    async def __aenter__(self) -> MagicMock:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


@pytest.mark.asyncio
async def test_get_workflows_by_organization_id_search_key_matches_workflow_permanent_id() -> None:
    captured: dict[str, Any] = {}

    class _Result:
        def all(self):
            return []

    async def _execute(query):
        captured["query"] = query
        return _Result()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_workflows_by_organization_id(
        organization_id="o_test",
        search_key="wpid_510867674757598984",
    )

    # Inspect the WHERE clause specifically — workflow_permanent_id is also in the
    # SELECT list, so a substring check on the full SQL would be a false positive.
    whereclause = captured["query"].whereclause
    assert whereclause is not None
    compiled_where = str(whereclause.compile(compile_kwargs={"literal_binds": True}))

    # The search filter must reference the workflow_permanent_id column directly so
    # that pasting a wpid_* into the workflows page search box finds the workflow.
    assert "workflows.workflow_permanent_id" in compiled_where
    assert "wpid_510867674757598984" in compiled_where


@pytest.mark.asyncio
async def test_get_workflow_by_permanent_id_bounds_the_latest_version_lookup(agent_db: AgentDB, db_engine: Any) -> None:
    """The latest-version lookup must emit LIMIT.

    Result.first() returns the newest row whether or not the statement is bounded, so a
    correctness assertion cannot catch this: unbounded, the query fetches and discards every
    version row for the wpid, and each discarded row carries a full workflow_definition.
    """
    org = await agent_db.organizations.create_organization(
        organization_name="Version Growth Org",
        domain="version-growth.test",
    )
    wpid = "wpid_version_growth"
    for version in range(1, 6):
        await agent_db.workflows.create_workflow(
            title=f"v{version}",
            workflow_definition={"parameters": [], "blocks": []},
            organization_id=org.organization_id,
            workflow_permanent_id=wpid,
            version=version,
        )

    statements: list[str] = []

    @event.listens_for(db_engine.sync_engine, "after_cursor_execute")
    def _capture(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        statements.append(statement)

    workflow = await agent_db.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=wpid,
        organization_id=org.organization_id,
    )

    assert workflow is not None
    assert workflow.version == 5

    version_lookups = [s for s in statements if "ORDER BY workflows.version DESC" in s]
    assert version_lookups, "expected the latest-version lookup to be issued"
    assert all("LIMIT" in s.upper() for s in version_lookups)


async def _seed_template_page(agent_db: AgentDB, organization_id: str, latest: Workflow) -> list[str]:
    foreign_org = await agent_db.organizations.create_organization(organization_name="Other Template Org")
    template_id = "wpid_page_template"
    plain_id = "wpid_page_plain"
    deleted_id = "wpid_page_deleted_template"
    foreign_id = "wpid_page_foreign_template"
    new_page_ids = [template_id, plain_id, deleted_id, foreign_id]
    off_page_ids = [f"wpid_off_page_{index:04d}" for index in range(1_000)]
    offsets = [*range(len(new_page_ids), 0, -1), *range(-1, -len(off_page_ids) - 1, -1)]
    workflow_rows = [
        {
            "organization_id": organization_id,
            "workflow_permanent_id": permanent_id,
            "title": permanent_id,
            "workflow_definition": {"parameters": [], "blocks": []},
            "created_at": latest.created_at + timedelta(seconds=offset),
            "modified_at": latest.created_at + timedelta(seconds=offset),
        }
        for permanent_id, offset in zip(new_page_ids + off_page_ids, offsets, strict=True)
    ]
    workflow_rows.append({**workflow_rows[len(new_page_ids) - 1], "organization_id": foreign_org.organization_id})
    active_template_ids = [template_id, latest.workflow_permanent_id, *off_page_ids]
    template_times = {"created_at": latest.created_at, "modified_at": latest.created_at}
    template_rows = [
        {"organization_id": organization_id, "workflow_permanent_id": permanent_id, **template_times}
        for permanent_id in active_template_ids
    ]
    template_rows.extend(
        [
            {
                "organization_id": organization_id,
                "workflow_permanent_id": deleted_id,
                "deleted_at": latest.created_at,
                **template_times,
            },
            {"organization_id": foreign_org.organization_id, "workflow_permanent_id": foreign_id, **template_times},
        ]
    )
    async with agent_db.Session() as session:
        await session.execute(insert(WorkflowModel), workflow_rows)
        await session.execute(insert(WorkflowTemplateModel), template_rows)
        await session.commit()
    return [*new_page_ids, latest.workflow_permanent_id]


@pytest.mark.asyncio
async def test_get_workflows_by_organization_id_reports_the_first_versions_creator_and_date(agent_db: AgentDB) -> None:
    """Every save inserts a new version row; the list must still report the original author and creation date."""
    org = await agent_db.organizations.create_organization(
        organization_name="Attribution Org",
        domain="attribution.test",
    )
    wpid = "wpid_attribution"
    first_version = await agent_db.workflows.create_workflow(
        title="v1",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org.organization_id,
        workflow_permanent_id=wpid,
        version=1,
        created_by="user_author",
        edited_by="user_author",
    )
    await agent_db.workflows.create_workflow(
        title="v2",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org.organization_id,
        workflow_permanent_id=wpid,
        version=2,
        created_by="user_editor",
        edited_by="user_editor",
    )

    [workflow] = await agent_db.workflows.get_workflows_by_organization_id(organization_id=org.organization_id)

    assert workflow.version == 2
    assert workflow.created_by == "user_editor"
    assert workflow.model_dump()["original_created_by"] == "user_author"
    assert workflow.original_created_at == first_version.created_at
    assert workflow.created_at != first_version.created_at

    await agent_db.workflows.soft_delete_workflow_by_id(first_version.workflow_id, org.organization_id)
    [workflow] = await agent_db.workflows.get_workflows_by_organization_id(organization_id=org.organization_id)

    assert workflow.original_created_at == first_version.created_at
    assert workflow.original_created_by == "user_author"
    assert await agent_db.workflows.get_workflow_original_creators({wpid}, org.organization_id) == {wpid: "user_author"}

    page_ids = await _seed_template_page(agent_db, org.organization_id, workflow)
    page = await agent_db.workflows.get_workflows_by_organization_id(
        organization_id=org.organization_id, page_size=len(page_ids)
    )
    assert [item.workflow_permanent_id for item in page] == page_ids
    assert [item.is_template for item in page] == [True, False, False, False, True]
    assert page[-1].created_by == "user_editor"
    assert page[-1].original_created_by == "user_author"
    assert page[-1].original_created_at == first_version.created_at

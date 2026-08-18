"""Regression tests for copilot attribution columns."""

from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import Base


@pytest_asyncio.fixture
async def db_engine() -> AsyncGenerator[Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def agent_db(db_engine: Any) -> AsyncGenerator[AgentDB]:
    yield AgentDB(database_string="sqlite+aiosqlite:///:memory:", debug_enabled=True, db_engine=db_engine)


@pytest_asyncio.fixture
async def org_id(agent_db: AgentDB) -> str:
    org = await agent_db.organizations.create_organization(
        organization_name="Attribution Org",
        domain="attribution.test",
    )
    return org.organization_id


@pytest.mark.asyncio
async def test_create_workflow_without_attribution_defaults_to_none(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="plain-create",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org_id,
    )
    assert workflow.created_by is None
    assert workflow.edited_by is None


@pytest.mark.asyncio
async def test_create_workflow_stamps_attribution_when_passed(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="copilot-create",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org_id,
        created_by="copilot",
        edited_by="copilot",
    )
    assert workflow.created_by == "copilot"
    assert workflow.edited_by == "copilot"


@pytest.mark.asyncio
async def test_update_workflow_omit_attribution_preserves_stamps(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="seed",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org_id,
        created_by="copilot",
        edited_by="copilot",
    )
    # Omit created_by / edited_by — the repo must NOT touch either column.
    await agent_db.workflows.update_workflow(
        workflow_id=workflow.workflow_id,
        organization_id=org_id,
        title="renamed",
    )
    reread = await agent_db.workflows.get_workflow(
        workflow_id=workflow.workflow_id,
        organization_id=org_id,
    )
    assert reread is not None
    assert reread.created_by == "copilot"
    assert reread.edited_by == "copilot"


@pytest.mark.asyncio
async def test_update_workflow_explicit_none_clears_attribution(agent_db: AgentDB, org_id: str) -> None:
    # _UNSET sentinel distinguishes omit (preserve) from None (clear); rollback relies on this.
    workflow = await agent_db.workflows.create_workflow(
        title="seed",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org_id,
        created_by="copilot",
        edited_by="copilot",
    )
    await agent_db.workflows.update_workflow(
        workflow_id=workflow.workflow_id,
        organization_id=org_id,
        created_by=None,
        edited_by=None,
    )
    reread = await agent_db.workflows.get_workflow(
        workflow_id=workflow.workflow_id,
        organization_id=org_id,
    )
    assert reread is not None
    assert reread.created_by is None
    assert reread.edited_by is None


@pytest.mark.asyncio
async def test_update_workflow_and_reconcile_explicit_none_clears_attribution(agent_db: AgentDB, org_id: str) -> None:
    # Reconcile path must honor the same omit/None semantics as update_workflow.
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition

    workflow = await agent_db.workflows.create_workflow(
        title="seed",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org_id,
        created_by="copilot",
        edited_by="copilot",
    )
    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=workflow.workflow_id,
        organization_id=org_id,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
        created_by=None,
        edited_by=None,
    )
    reread = await agent_db.workflows.get_workflow(
        workflow_id=workflow.workflow_id,
        organization_id=org_id,
    )
    assert reread is not None
    assert reread.created_by is None
    assert reread.edited_by is None


@pytest.mark.asyncio
async def test_create_workflow_run_without_session_id_defaults_to_none(agent_db: AgentDB, org_id: str) -> None:
    # No ambient skyvern_context; no explicit param — copilot_session_id stays NULL.
    workflow = await agent_db.workflows.create_workflow(
        title="wf",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org_id,
    )
    run = await agent_db.workflow_runs.create_workflow_run(
        workflow_permanent_id=workflow.workflow_permanent_id,
        workflow_id=workflow.workflow_id,
        organization_id=org_id,
    )
    assert run.copilot_session_id is None


@pytest.mark.asyncio
async def test_create_workflow_run_explicit_session_id_persists(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="wf",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org_id,
    )
    run = await agent_db.workflow_runs.create_workflow_run(
        workflow_permanent_id=workflow.workflow_permanent_id,
        workflow_id=workflow.workflow_id,
        organization_id=org_id,
        copilot_session_id="chat_abc123",
    )
    assert run.copilot_session_id == "chat_abc123"


@pytest.mark.asyncio
async def test_create_workflow_run_ignores_ambient_context(agent_db: AgentDB, org_id: str) -> None:
    # Ambient-context resolution lives in the service layer, not the repo. Repo trusts the param.
    workflow = await agent_db.workflows.create_workflow(
        title="wf",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org_id,
    )
    ambient = skyvern_context.SkyvernContext(copilot_session_id="chat_from_ctx")
    with skyvern_context.scoped(ambient):
        run = await agent_db.workflow_runs.create_workflow_run(
            workflow_permanent_id=workflow.workflow_permanent_id,
            workflow_id=workflow.workflow_id,
            organization_id=org_id,
        )
    assert run.copilot_session_id is None


@pytest.mark.asyncio
async def test_copilot_authorship_resolves_from_edited_by_alone(agent_db: AgentDB, org_id: str) -> None:
    """Copilot lineage survives without a created_by='copilot' stamp: the copilot writes
    edited_by unconditionally, while created_by keeps the user who created the workflow."""
    workflow = await agent_db.workflows.create_workflow(
        title="user-created-copilot-edited",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org_id,
        created_by=f"{org_id}_user",
        edited_by="copilot",
    )
    assert workflow.created_by == f"{org_id}_user"
    assert await agent_db.workflows.is_workflow_copilot_authored(
        workflow_permanent_id=workflow.workflow_permanent_id,
        organization_id=org_id,
    )


@pytest.mark.asyncio
async def test_workflow_never_touched_by_copilot_is_not_copilot_authored(agent_db: AgentDB, org_id: str) -> None:
    workflow = await agent_db.workflows.create_workflow(
        title="user-only",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org_id,
        created_by=f"{org_id}_user",
        edited_by=f"{org_id}_user",
    )
    assert not await agent_db.workflows.is_workflow_copilot_authored(
        workflow_permanent_id=workflow.workflow_permanent_id,
        organization_id=org_id,
    )

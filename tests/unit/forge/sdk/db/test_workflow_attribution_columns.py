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


# ---------------------------------------------------------------------------
# Stub-heuristic regression coverage
# ---------------------------------------------------------------------------


def _make_workflow_stub(*, version: int, created_by: str | None, block_count: int) -> Any:
    blocks = [object()] * block_count
    definition = type("D", (), {"blocks": blocks})()
    return type(
        "W",
        (),
        {"version": version, "created_by": created_by, "workflow_definition": definition},
    )()


def test_is_copilot_born_stub_true_on_version_one_empty_unstamped() -> None:
    from skyvern.forge.sdk.copilot.attribution import is_copilot_born_initial_write

    wf = _make_workflow_stub(version=1, created_by=None, block_count=0)
    assert is_copilot_born_initial_write(wf) is True


def test_is_copilot_born_stub_false_on_later_version() -> None:
    # v1 is the only version that can be copilot-born; cleared v2+ would otherwise false-positive.
    from skyvern.forge.sdk.copilot.attribution import is_copilot_born_initial_write

    wf = _make_workflow_stub(version=2, created_by=None, block_count=0)
    assert is_copilot_born_initial_write(wf) is False


def test_is_copilot_born_stub_false_on_already_stamped() -> None:
    from skyvern.forge.sdk.copilot.attribution import is_copilot_born_initial_write

    wf = _make_workflow_stub(version=1, created_by="copilot", block_count=0)
    assert is_copilot_born_initial_write(wf) is False


def test_is_copilot_born_stub_false_on_non_empty_definition() -> None:
    from skyvern.forge.sdk.copilot.attribution import is_copilot_born_initial_write

    wf = _make_workflow_stub(version=1, created_by=None, block_count=3)
    assert is_copilot_born_initial_write(wf) is False


def test_is_copilot_born_stub_false_on_none() -> None:
    from skyvern.forge.sdk.copilot.attribution import is_copilot_born_initial_write

    assert is_copilot_born_initial_write(None) is False

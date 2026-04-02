from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import Base
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameterType


@pytest_asyncio.fixture
async def db_engine() -> AsyncGenerator[Any, None]:
    # Use an in-memory SQLite database for testing
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def agent_db(db_engine: Any) -> AsyncGenerator[AgentDB, None]:
    yield AgentDB(database_string="sqlite+aiosqlite:///:memory:", debug_enabled=True, db_engine=db_engine)


@pytest.mark.asyncio
async def test_create_organization(agent_db: AgentDB) -> None:
    org_name = "Test Organization"
    domain = "test.com"
    organization = await agent_db.create_organization(organization_name=org_name, domain=domain)
    assert organization is not None
    assert organization.organization_name == org_name
    assert organization.domain == domain

    retrieved_org = await agent_db.get_organization(organization.organization_id)
    assert retrieved_org is not None
    assert retrieved_org.organization_name == org_name
    assert retrieved_org.domain == domain

    retrieved_by_domain = await agent_db.get_organization_by_domain(domain=domain)
    assert retrieved_by_domain is not None
    assert retrieved_by_domain.organization_name == org_name
    assert retrieved_by_domain.domain == domain


@pytest.mark.asyncio
async def test_create_organization_with_explicit_id(agent_db: AgentDB) -> None:
    organization = await agent_db.create_organization(
        organization_id="o_test_org",
        organization_name="Explicit Id Organization",
        domain="explicit.test",
    )

    assert organization.organization_id == "o_test_org"

    retrieved_org = await agent_db.get_organization("o_test_org")
    assert retrieved_org is not None
    assert retrieved_org.organization_name == "Explicit Id Organization"
    assert retrieved_org.domain == "explicit.test"


@pytest.mark.asyncio
async def test_get_organization_not_found(agent_db: AgentDB) -> None:
    retrieved_org = await agent_db.get_organization("non_existent_id")
    assert retrieved_org is None

    retrieved_by_domain = await agent_db.get_organization_by_domain(domain="nonexistent.com")
    assert retrieved_by_domain is None


@pytest.mark.asyncio
async def test_create_workflow_run_parameters_persists_all_values(agent_db: AgentDB) -> None:
    organization = await agent_db.create_organization(
        organization_name="Workflow Parameter Org",
        domain="workflow-params.test",
    )
    workflow = await agent_db.create_workflow(
        title="Workflow Parameter Test",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=organization.organization_id,
    )
    workflow_run = await agent_db.create_workflow_run(
        workflow_permanent_id=workflow.workflow_permanent_id,
        workflow_id=workflow.workflow_id,
        organization_id=organization.organization_id,
    )

    url_parameter = await agent_db.create_workflow_parameter(
        workflow_id=workflow.workflow_id,
        workflow_parameter_type=WorkflowParameterType.STRING,
        key="url",
        default_value=None,
    )
    count_parameter = await agent_db.create_workflow_parameter(
        workflow_id=workflow.workflow_id,
        workflow_parameter_type=WorkflowParameterType.INTEGER,
        key="count",
        default_value=None,
    )

    created_parameters = await agent_db.create_workflow_run_parameters(
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_parameter_values=[
            (url_parameter, "https://example.com"),
            (count_parameter, "7"),
        ],
    )

    assert [parameter.value for parameter in created_parameters] == ["https://example.com", 7]
    assert all(parameter.created_at is not None for parameter in created_parameters)

    stored_parameters = await agent_db.get_workflow_run_parameters(workflow_run.workflow_run_id)
    assert len(stored_parameters) == 2
    assert {parameter.key: run_parameter.value for parameter, run_parameter in stored_parameters} == {
        "url": "https://example.com",
        "count": 7,
    }

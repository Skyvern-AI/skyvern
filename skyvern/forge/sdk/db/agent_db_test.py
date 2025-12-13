from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import Base


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
async def test_get_organization_not_found(agent_db: AgentDB) -> None:
    retrieved_org = await agent_db.get_organization("non_existent_id")
    assert retrieved_org is None

    retrieved_by_domain = await agent_db.get_organization_by_domain(domain="nonexistent.com")
    assert retrieved_by_domain is None

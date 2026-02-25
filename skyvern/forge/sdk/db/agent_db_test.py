import datetime
from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import Column, DateTime, String, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB, db_operation
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import Base, SoftDeleteMixin


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


# --- db_operation decorator tests ---


class TestDbOperation:
    @pytest.mark.asyncio
    async def test_passes_through_return_value(self) -> None:
        @db_operation("test_op")
        async def op() -> str:
            return "ok"

        assert await op() == "ok"

    @pytest.mark.asyncio
    async def test_passes_through_not_found_error(self) -> None:
        @db_operation("test_op")
        async def op() -> None:
            raise NotFoundError("missing")

        with pytest.raises(NotFoundError):
            await op()

    @pytest.mark.asyncio
    async def test_passes_through_value_error(self) -> None:
        @db_operation("test_op")
        async def op() -> None:
            raise ValueError("bad input")

        with pytest.raises(ValueError):
            await op()

    @pytest.mark.asyncio
    async def test_logs_and_reraises_sqlalchemy_error(self) -> None:
        @db_operation("test_op")
        async def op() -> None:
            raise SQLAlchemyError("db error")

        with pytest.raises(SQLAlchemyError):
            await op()

    @pytest.mark.asyncio
    async def test_logs_and_reraises_unexpected_error(self) -> None:
        @db_operation("test_op")
        async def op() -> None:
            raise RuntimeError("unexpected")

        with pytest.raises(RuntimeError):
            await op()


# --- SoftDeleteMixin tests ---


class _TestSoftDeleteModel(SoftDeleteMixin, Base):
    __tablename__ = "_test_soft_delete"
    id = Column(String, primary_key=True)
    deleted_at = Column(DateTime, nullable=True)


class TestSoftDeleteMixin:
    @pytest.mark.asyncio
    async def test_exclude_deleted_filters_deleted_rows(self, db_engine: Any) -> None:
        async with db_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        db = BaseAlchemyDB(db_engine)
        async with db.Session() as session:
            session.add(_TestSoftDeleteModel(id="alive", deleted_at=None))
            session.add(_TestSoftDeleteModel(id="dead", deleted_at=datetime.datetime.utcnow()))
            await session.commit()

        async with db.Session() as session:
            query = _TestSoftDeleteModel.exclude_deleted(select(_TestSoftDeleteModel))
            results = (await session.scalars(query)).all()
            assert len(results) == 1
            assert results[0].id == "alive"

    @pytest.mark.asyncio
    async def test_mark_deleted_sets_timestamp(self, db_engine: Any) -> None:
        async with db_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        db = BaseAlchemyDB(db_engine)
        async with db.Session() as session:
            obj = _TestSoftDeleteModel(id="to_delete", deleted_at=None)
            session.add(obj)
            await session.commit()

            obj.mark_deleted()
            await session.commit()
            await session.refresh(obj)
            assert obj.deleted_at is not None

    def test_soft_delete_values_returns_dict(self) -> None:
        values = _TestSoftDeleteModel.soft_delete_values()
        assert "deleted_at" in values
        assert isinstance(values["deleted_at"], datetime.datetime)


# --- Existing AgentDB integration tests ---


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

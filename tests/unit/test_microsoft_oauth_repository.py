import datetime
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import Base, MicrosoftOAuthCredentialModel  # noqa: F401 - registers model on Base
from skyvern.forge.sdk.db.repositories.microsoft_oauth import STATE_ACTIVE, STATE_REVOKED, MicrosoftOAuthRepository


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def repo(engine: AsyncEngine) -> MicrosoftOAuthRepository:
    db = BaseAlchemyDB(engine)
    return MicrosoftOAuthRepository(db.Session, debug_enabled=False)


@pytest.mark.asyncio
async def test_update_email_address_only_if_null_does_not_overwrite_existing_address(
    repo: MicrosoftOAuthRepository,
    engine: AsyncEngine,
) -> None:
    modified_at = datetime.datetime(2026, 7, 30, 12, 0, 0)
    async with engine.begin() as conn:
        await conn.execute(
            MicrosoftOAuthCredentialModel.__table__.insert().values(
                id="msoac_email",
                organization_id="o_test",
                credential_name="Default",
                state=STATE_ACTIVE,
                email_address="fresh@example.test",
                created_at=modified_at,
                modified_at=modified_at,
            )
        )

    updated = await repo.update_email_address(
        organization_id="o_test",
        credential_id="msoac_email",
        email_address="stale@example.test",
        only_if_null=True,
    )

    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(
                    MicrosoftOAuthCredentialModel.email_address,
                    MicrosoftOAuthCredentialModel.modified_at,
                ).where(MicrosoftOAuthCredentialModel.id == "msoac_email")
            )
        ).one()

    assert stored.email_address == "fresh@example.test"
    assert stored.modified_at == modified_at
    assert updated is False


@pytest.mark.asyncio
async def test_update_email_address_authoritative_write_updates_address(
    repo: MicrosoftOAuthRepository,
    engine: AsyncEngine,
) -> None:
    modified_at = datetime.datetime(2026, 7, 30, 12, 0, 0)
    async with engine.begin() as conn:
        await conn.execute(
            MicrosoftOAuthCredentialModel.__table__.insert().values(
                id="msoac_email",
                organization_id="o_test",
                credential_name="Default",
                state=STATE_ACTIVE,
                email_address="old@example.test",
                created_at=modified_at,
                modified_at=modified_at,
            )
        )

    updated = await repo.update_email_address(
        organization_id="o_test",
        credential_id="msoac_email",
        email_address="fresh@example.test",
        only_if_null=False,
    )

    async with engine.connect() as conn:
        stored_address = (
            await conn.execute(
                select(MicrosoftOAuthCredentialModel.email_address).where(
                    MicrosoftOAuthCredentialModel.id == "msoac_email"
                )
            )
        ).scalar_one()

    assert stored_address == "fresh@example.test"
    assert updated is True


@pytest.mark.asyncio
async def test_mark_revoked_and_scrub_clears_email_address(
    repo: MicrosoftOAuthRepository,
    engine: AsyncEngine,
) -> None:
    now = datetime.datetime(2026, 7, 30, 12, 0, 0)
    async with engine.begin() as conn:
        await conn.execute(
            MicrosoftOAuthCredentialModel.__table__.insert().values(
                id="msoac_revoke_email",
                organization_id="o_test",
                credential_name="Default",
                state=STATE_ACTIVE,
                email_address="account@example.test",
                created_at=now,
                modified_at=now,
            )
        )

    await repo.mark_revoked_and_scrub(
        organization_id="o_test",
        credential_id="msoac_revoke_email",
        now=now + datetime.timedelta(minutes=1),
    )

    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(
                    MicrosoftOAuthCredentialModel.state,
                    MicrosoftOAuthCredentialModel.email_address,
                ).where(MicrosoftOAuthCredentialModel.id == "msoac_revoke_email")
            )
        ).one()

    assert stored.state == STATE_REVOKED
    assert stored.email_address is None

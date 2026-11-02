import datetime
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import Base, MicrosoftOAuthCredentialModel
from skyvern.forge.sdk.db.repositories.microsoft_oauth import STATE_ACTIVE, STATE_REVOKED, MicrosoftOAuthRepository
from skyvern.forge.sdk.encrypt.base import EncryptMethod


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
    modified_at = datetime.datetime.fromisoformat("2026-07-30T12:00:00")
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
    modified_at = datetime.datetime.fromisoformat("2026-07-30T12:00:00")
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
async def test_powerbi_rows_are_listed_and_managed_only_by_their_owner(
    repo: MicrosoftOAuthRepository,
    engine: AsyncEngine,
) -> None:
    now = datetime.datetime.fromisoformat("2026-10-28T12:00:00")
    powerbi_scopes = ["https://analysis.windows.net/powerbi/api/Dataset.Read.All"]
    async with engine.begin() as conn:
        await conn.execute(
            MicrosoftOAuthCredentialModel.__table__.insert(),
            [
                {
                    "id": "powerbi_owner",
                    "organization_id": "o_test",
                    "credential_name": "Owner Power BI",
                    "state": STATE_ACTIVE,
                    "scopes_requested": powerbi_scopes,
                    "scopes_granted": ["Dataset.Read.All"],
                    "encrypted_refresh_token": "pbi-encrypted-token",
                    "initiating_user_id": "user-a",
                    "connection_id": "powerbi_owner",
                    "created_at": now,
                    "modified_at": now,
                },
                {
                    "id": "powerbi_ownerless",
                    "organization_id": "o_test",
                    "credential_name": "Ownerless Power BI",
                    "state": STATE_ACTIVE,
                    "scopes_requested": powerbi_scopes,
                    "scopes_granted": ["Dataset.Read.All"],
                    "encrypted_refresh_token": None,
                    "initiating_user_id": None,
                    "connection_id": "powerbi_ownerless",
                    "created_at": now,
                    "modified_at": now,
                },
                {
                    "id": "powerbi_synthetic_owner",
                    "organization_id": "o_test",
                    "credential_name": "Synthetic Power BI",
                    "state": STATE_ACTIVE,
                    "scopes_requested": powerbi_scopes,
                    "scopes_granted": ["Dataset.Read.All"],
                    "encrypted_refresh_token": None,
                    "initiating_user_id": "o_test_user",
                    "connection_id": "powerbi_synthetic_owner",
                    "created_at": now,
                    "modified_at": now,
                },
                {
                    "id": "outlook_legacy",
                    "organization_id": "o_test",
                    "credential_name": "Legacy Outlook",
                    "state": STATE_ACTIVE,
                    "scopes_requested": ["Mail.Read"],
                    "scopes_granted": ["Mail.Read"],
                    "encrypted_refresh_token": None,
                    "initiating_user_id": "user-a",
                    "connection_id": None,
                    "created_at": now,
                    "modified_at": now,
                },
            ],
        )

    owner_rows = await repo.list_active_for_org("o_test", user_id="user-a")
    other_rows = await repo.list_active_for_org("o_test", user_id="user-b")
    ownerless_rows = await repo.list_active_for_org("o_test")
    synthetic_rows = await repo.list_active_for_org("o_test", user_id="o_test_user")
    owner_metadata = await repo.load_active_access_metadata("o_test", "powerbi_owner")

    assert {row.id for row in owner_rows} == {"powerbi_owner", "outlook_legacy"}
    assert {row.id for row in other_rows} == {"outlook_legacy"}
    assert {row.id for row in ownerless_rows} == {"outlook_legacy"}
    assert {row.id for row in synthetic_rows} == {"outlook_legacy"}
    assert await repo.can_manage_credential("o_test", "powerbi_owner", "user-a") is True
    assert await repo.can_manage_credential("o_test", "powerbi_owner", "user-b") is False
    assert await repo.can_manage_credential("o_test", "powerbi_ownerless", "user-a") is False
    assert await repo.can_manage_credential("o_test", "powerbi_synthetic_owner", "o_test_user") is False
    assert await repo.can_manage_credential("o_test", "outlook_legacy", "user-b") is True
    assert await repo.can_manage_credential("o_other", "powerbi_owner", "user-a") is False
    assert owner_metadata is not None
    assert owner_metadata.initiating_user_id == "user-a"
    assert owner_metadata.connection_id == "powerbi_owner"
    assert await repo.load_active_access_metadata("o_other", "powerbi_owner") is None


@pytest.mark.asyncio
async def test_update_active_refresh_token_only_rewrites_expected_ciphertext(
    repo: MicrosoftOAuthRepository,
    engine: AsyncEngine,
) -> None:
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    async with engine.begin() as conn:
        await conn.execute(
            MicrosoftOAuthCredentialModel.__table__.insert().values(
                id="msoac_rotation",
                organization_id="o_test",
                credential_name="Default",
                state=STATE_ACTIVE,
                encrypted_refresh_token="encrypted-old",
                encrypted_method="aes",
                created_at=now,
                modified_at=now,
            )
        )

    rotated = await repo.update_active_refresh_token(
        organization_id="o_test",
        credential_id="msoac_rotation",
        encrypted_refresh_token="encrypted-rotated",
        encrypted_method=EncryptMethod.AES,
        expected_encrypted_refresh_token="encrypted-old",
        now=now + datetime.timedelta(minutes=1),
    )
    stale = await repo.update_active_refresh_token(
        organization_id="o_test",
        credential_id="msoac_rotation",
        encrypted_refresh_token="encrypted-stale",
        encrypted_method=EncryptMethod.AES,
        expected_encrypted_refresh_token="encrypted-old",
        now=now + datetime.timedelta(minutes=2),
    )

    async with engine.connect() as conn:
        stored = await conn.scalar(
            select(MicrosoftOAuthCredentialModel.encrypted_refresh_token).where(
                MicrosoftOAuthCredentialModel.id == "msoac_rotation"
            )
        )

    assert rotated is True
    assert stale is False
    assert stored == "encrypted-rotated"


@pytest.mark.asyncio
async def test_mark_revoked_and_scrub_clears_email_address(
    repo: MicrosoftOAuthRepository,
    engine: AsyncEngine,
) -> None:
    now = datetime.datetime.fromisoformat("2026-07-30T12:00:00")
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

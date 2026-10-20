import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skyvern.forge.sdk.db.models import Base, CredentialModel
from skyvern.forge.sdk.db.repositories.credentials import CredentialRepository
from skyvern.forge.sdk.schemas.credentials import CredentialType, CredentialVaultType

ORG_A = "o_aaaaaaaaaaaaaaa"
ORG_B = "o_bbbbbbbbbbbbbbb"


@pytest_asyncio.fixture
async def repo_and_session() -> tuple:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[CredentialModel.__table__]))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield CredentialRepository(session_factory), session_factory
    finally:
        await engine.dispose()


async def _add_credential(session_factory, organization_id: str, name: str) -> str:
    async with session_factory() as session:
        credential = CredentialModel(
            organization_id=organization_id,
            name=name,
            credential_type="password",
            vault_type="custom",
            item_id="item",
            username="user@example.com",
            totp_type="none",
        )
        session.add(credential)
        await session.commit()
        await session.refresh(credential)
        return credential.credential_id


@pytest.mark.asyncio
async def test_search_matches_an_exact_credential_id_within_the_org(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    portal = await _add_credential(session_factory, ORG_A, "Portal login")
    await _add_credential(session_factory, ORG_A, "Billing login")

    async def found(organization_id: str, search: str) -> list[str]:
        return [c.credential_id for c in await repo.get_credentials(organization_id, search=search)]

    assert await found(ORG_A, portal) == [portal]
    assert await found(ORG_A, f"  {portal} ") == [portal]
    assert await found(ORG_B, portal) == []
    assert await found(ORG_A, "cred") == []
    assert await found(ORG_A, "portal") == [portal]


@pytest.mark.asyncio
async def test_vault_item_link_is_unique_per_organization_and_round_trips(repo_and_session) -> None:
    repo, _session_factory = repo_and_session

    async def create(organization_id: str, vault_id: str | None) -> str:
        credential = await repo.create_credential(
            organization_id=organization_id,
            name="Imported credential",
            vault_type=CredentialVaultType.ONEPASSWORD,
            vault_id=vault_id,
            item_id="item-1",
            credential_type=CredentialType.PASSWORD,
            username="user",
            totp_type="none",
            card_last4=None,
            card_brand=None,
        )
        return credential.credential_id

    credential_id = await create(ORG_A, "vault-1")
    credential = await repo.get_credential(credential_id, ORG_A)
    assert credential is not None
    assert credential.vault_id == "vault-1"

    with pytest.raises(IntegrityError):
        await create(ORG_A, "vault-1")

    await repo.delete_credential(credential_id, ORG_A)
    await create(ORG_A, "vault-1")
    await create(ORG_A, "vault-2")
    await create(ORG_B, "vault-1")
    await create(ORG_A, None)
    await create(ORG_A, None)


@pytest.mark.asyncio
async def test_onepassword_metadata_upsert_is_idempotent_and_stale_state_clears(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    created, was_created = await repo.upsert_onepassword_credential_metadata(
        organization_id=ORG_A,
        vault_id="vault-1",
        item_id="item-1",
        name="Original title",
        credential_type=CredentialType.PASSWORD,
        tested_url="https://example.test/login",
        folder_id=None,
        created_by="user-1",
    )
    assert was_created
    assert created.vault_type == CredentialVaultType.ONEPASSWORD
    assert created.vault_id == "vault-1" and created.item_id == "item-1"

    updated, was_created = await repo.upsert_onepassword_credential_metadata(
        organization_id=ORG_A,
        vault_id="vault-1",
        item_id="item-1",
        name="Updated title",
        credential_type=CredentialType.CREDIT_CARD,
        tested_url="https://example.test/card",
        folder_id=None,
        created_by="user-2",
    )
    assert not was_created
    assert updated.credential_id == created.credential_id
    assert updated.name == "Updated title"
    assert updated.credential_type == CredentialType.CREDIT_CARD
    assert updated.created_by == "user-1"
    assert await repo.get_onepassword_credentials(ORG_A) == [updated]

    await repo.set_onepassword_credentials_stale(ORG_A, [(updated.credential_id, None)], stale=True)
    async with session_factory() as session:
        stale_at = await session.scalar(
            select(CredentialModel.upstream_missing_at).where(CredentialModel.credential_id == updated.credential_id)
        )
    assert stale_at is not None

    refreshed, was_created = await repo.upsert_onepassword_credential_metadata(
        organization_id=ORG_A,
        vault_id="vault-1",
        item_id="item-1",
        name="Restored title",
        credential_type=CredentialType.CREDIT_CARD,
        tested_url="https://example.test/card",
        folder_id=None,
        created_by="user-2",
    )
    assert not was_created
    async with session_factory() as session:
        stale_at = await session.scalar(
            select(CredentialModel.upstream_missing_at).where(CredentialModel.credential_id == refreshed.credential_id)
        )
    assert stale_at is None

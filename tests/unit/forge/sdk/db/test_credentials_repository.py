import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skyvern.forge.sdk.db.models import Base, CredentialModel
from skyvern.forge.sdk.db.repositories.credentials import CredentialRepository

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

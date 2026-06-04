"""Behavioral coverage for CredentialFoldersRepository.

Runs against an in-memory SQLite DB so the org-isolation and soft-delete
semantics are exercised for real — a regression that dropped an organization
filter or that soft-deleted members instead of detaching them would fail here
rather than slip through the instantiation smoke tests.
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skyvern.forge.sdk.db.models import Base, CredentialFolderModel, CredentialModel
from skyvern.forge.sdk.db.repositories.credential_folders import CredentialFoldersRepository

ORG_A = "o_aaaaaaaaaaaaaaa"
ORG_B = "o_bbbbbbbbbbbbbbb"


@pytest_asyncio.fixture
async def repo_and_session() -> tuple:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[CredentialFolderModel.__table__, CredentialModel.__table__],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield CredentialFoldersRepository(session_factory), session_factory
    finally:
        await engine.dispose()


async def _add_credential(session_factory, organization_id: str, name: str = "cred") -> str:
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
async def test_set_credential_folder_rejects_other_orgs_credential(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    folder = await repo.create_credential_folder(ORG_A, "Prod")
    cred_b = await _add_credential(session_factory, ORG_B)
    # ORG_A may not move a credential it doesn't own.
    assert await repo.set_credential_folder(cred_b, ORG_A, folder.folder_id) is None


@pytest.mark.asyncio
async def test_set_credential_folder_rejects_other_orgs_folder(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    folder_b = await repo.create_credential_folder(ORG_B, "OtherOrg")
    cred_a = await _add_credential(session_factory, ORG_A)
    # Assigning into a folder from another org is rejected, not silently applied.
    with pytest.raises(ValueError):
        await repo.set_credential_folder(cred_a, ORG_A, folder_b.folder_id)


@pytest.mark.asyncio
async def test_soft_delete_detaches_members_without_deleting_them(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    folder = await repo.create_credential_folder(ORG_A, "Prod")
    cred = await _add_credential(session_factory, ORG_A)
    await repo.set_credential_folder(cred, ORG_A, folder.folder_id)
    assert await repo.get_credential_folder_credential_count(folder.folder_id, ORG_A) == 1

    assert await repo.soft_delete_credential_folder(folder.folder_id, ORG_A) is True
    assert await repo.get_credential_folder(folder.folder_id, ORG_A) is None

    async with session_factory() as session:
        refreshed = await session.get(CredentialModel, cred)
        assert refreshed.folder_id is None  # detached
        assert refreshed.deleted_at is None  # but NOT deleted (vault secret preserved)


@pytest.mark.asyncio
async def test_set_credential_folder_treats_empty_string_as_detach(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    folder = await repo.create_credential_folder(ORG_A, "Prod")
    cred = await _add_credential(session_factory, ORG_A)
    await repo.set_credential_folder(cred, ORG_A, folder.folder_id)
    assert await repo.get_credential_folder_credential_count(folder.folder_id, ORG_A) == 1

    # A cleared <select> can submit "" — it must detach, never write an empty FK or raise.
    result = await repo.set_credential_folder(cred, ORG_A, "")
    assert result is not None
    assert result.folder_id is None
    assert await repo.get_credential_folder_credential_count(folder.folder_id, ORG_A) == 0


@pytest.mark.asyncio
async def test_credential_counts_batch_returns_integer_counts(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    busy = await repo.create_credential_folder(ORG_A, "Busy")
    empty = await repo.create_credential_folder(ORG_A, "Empty")
    for _ in range(2):
        await repo.set_credential_folder(await _add_credential(session_factory, ORG_A), ORG_A, busy.folder_id)

    counts = await repo.get_credential_folder_credential_counts_batch([busy.folder_id, empty.folder_id], ORG_A)
    # Values must be real ints; reading Row.count would return the tuple method instead.
    assert counts[busy.folder_id] == 2
    assert all(isinstance(v, int) for v in counts.values())
    # Folders with no members are simply absent from the mapping.
    assert empty.folder_id not in counts


@pytest.mark.asyncio
async def test_listing_and_counts_are_org_scoped(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    folder_a = await repo.create_credential_folder(ORG_A, "A-folder")
    await repo.create_credential_folder(ORG_B, "B-folder")
    await repo.set_credential_folder(await _add_credential(session_factory, ORG_A), ORG_A, folder_a.folder_id)

    a_folders = await repo.get_credential_folders(ORG_A)
    assert [f.title for f in a_folders] == ["A-folder"]
    # A folder is invisible to / uncounted by another org.
    assert await repo.get_credential_folder(folder_a.folder_id, ORG_B) is None
    assert await repo.get_credential_folder_credential_count(folder_a.folder_id, ORG_B) == 0

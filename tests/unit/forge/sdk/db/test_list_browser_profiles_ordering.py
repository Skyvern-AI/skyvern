"""Browser-profile list ordering, exercised through the real query on SQLite.
Selectors paginate this endpoint, so newest-first must come from the database.
"""

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skyvern.forge.sdk.db.models import Base, BrowserProfileModel
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository

ORG = "o_ordering"


@pytest_asyncio.fixture
async def repo_and_session() -> tuple:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[BrowserProfileModel.__table__],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield BrowserSessionsRepository(session_factory), session_factory
    finally:
        await engine.dispose()


async def _add_profile(session_factory, name: str, created_at: datetime) -> str:
    async with session_factory() as session:
        profile = BrowserProfileModel(
            browser_profile_id=f"bp_{name}",
            organization_id=ORG,
            name=name,
            created_at=created_at,
            modified_at=created_at,
        )
        session.add(profile)
        await session.commit()
        return profile.browser_profile_id


@pytest.mark.asyncio
async def test_list_returns_newest_first(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    await _add_profile(session_factory, "oldest", datetime(2026, 1, 1))
    await _add_profile(session_factory, "middle", datetime(2026, 1, 2))
    await _add_profile(session_factory, "newest", datetime(2026, 1, 3))

    profiles = await repo.list_browser_profiles(organization_id=ORG)

    assert [p.name for p in profiles] == ["newest", "middle", "oldest"]


@pytest.mark.asyncio
async def test_ties_break_deterministically_by_id(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    same_time = datetime(2026, 1, 1)
    await _add_profile(session_factory, "tie_a", same_time)
    await _add_profile(session_factory, "tie_b", same_time)

    profiles = await repo.list_browser_profiles(organization_id=ORG)

    # Equal created_at, so the desc(id) tie-break decides: bp_tie_b > bp_tie_a.
    assert [p.name for p in profiles] == ["tie_b", "tie_a"]


@pytest.mark.asyncio
async def test_pagination_walks_from_newest_to_oldest(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    await _add_profile(session_factory, "oldest", datetime(2026, 1, 1))
    await _add_profile(session_factory, "middle", datetime(2026, 1, 2))
    await _add_profile(session_factory, "newest", datetime(2026, 1, 3))

    page_1 = await repo.list_browser_profiles(organization_id=ORG, page=1, page_size=1)
    page_2 = await repo.list_browser_profiles(organization_id=ORG, page=2, page_size=1)
    page_3 = await repo.list_browser_profiles(organization_id=ORG, page=3, page_size=1)

    assert [p.name for p in page_1] == ["newest"]
    assert [p.name for p in page_2] == ["middle"]
    assert [p.name for p in page_3] == ["oldest"]

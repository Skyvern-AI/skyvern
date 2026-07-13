"""Browser-profile list ordering, exercised through the real query on SQLite.
Selectors paginate this endpoint, so newest-first must come from the database.
"""

from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
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


async def _add_profile(
    session_factory,
    name: str,
    created_at: datetime,
    *,
    profile_id: str | None = None,
    is_managed: bool = False,
    workflow_permanent_id: str | None = None,
    browser_profile_key_digest: str | None = None,
    deleted_at: datetime | None = None,
) -> str:
    async with session_factory() as session:
        profile = BrowserProfileModel(
            browser_profile_id=profile_id or f"bp_{name}",
            organization_id=ORG,
            name=name,
            created_at=created_at,
            modified_at=created_at,
            is_managed=is_managed,
            workflow_permanent_id=workflow_permanent_id,
            browser_profile_key_digest=browser_profile_key_digest,
            deleted_at=deleted_at,
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


@pytest.mark.asyncio
async def test_list_filters_managed_profiles(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    await _add_profile(session_factory, "user", datetime(2026, 1, 1))
    await _add_profile(
        session_factory,
        "managed",
        datetime(2026, 1, 2),
        is_managed=True,
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest="",
    )

    all_profiles = await repo.list_browser_profiles(organization_id=ORG)
    user_profiles = await repo.list_browser_profiles(organization_id=ORG, managed=False)
    managed_profiles = await repo.list_browser_profiles(organization_id=ORG, managed=True)

    assert [p.name for p in all_profiles] == ["managed", "user"]
    assert [p.name for p in user_profiles] == ["user"]
    assert [p.name for p in managed_profiles] == ["managed"]


@pytest.mark.asyncio
async def test_sqlite_partial_index_allows_managed_profile_name_to_match_user_profile(repo_and_session) -> None:
    _, session_factory = repo_and_session
    await _add_profile(session_factory, "shared", datetime(2026, 1, 1), profile_id="bp_user")

    await _add_profile(
        session_factory,
        "shared",
        datetime(2026, 1, 2),
        profile_id="bp_managed",
        is_managed=True,
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest="",
    )


@pytest.mark.asyncio
async def test_sqlite_partial_index_allows_recreating_soft_deleted_managed_segment(repo_and_session) -> None:
    _, session_factory = repo_and_session
    await _add_profile(
        session_factory,
        "managed old",
        datetime(2026, 1, 1),
        profile_id="bp_managed_old",
        is_managed=True,
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest="digest",
        deleted_at=datetime(2026, 1, 2),
    )

    await _add_profile(
        session_factory,
        "managed new",
        datetime(2026, 1, 3),
        profile_id="bp_managed_new",
        is_managed=True,
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest="digest",
    )


@pytest.mark.asyncio
async def test_list_managed_browser_profiles_for_workflow_returns_active_managed_rows(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    await _add_profile(session_factory, "user", datetime(2026, 1, 1), workflow_permanent_id="wpid_test")
    await _add_profile(
        session_factory,
        "managed active",
        datetime(2026, 1, 2),
        is_managed=True,
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest="",
    )
    await _add_profile(
        session_factory,
        "managed deleted",
        datetime(2026, 1, 3),
        is_managed=True,
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest="deleted",
        deleted_at=datetime(2026, 1, 4),
    )
    await _add_profile(
        session_factory,
        "managed other",
        datetime(2026, 1, 5),
        is_managed=True,
        workflow_permanent_id="wpid_other",
        browser_profile_key_digest="",
    )

    profiles = await repo.list_managed_browser_profiles_for_workflow(
        organization_id=ORG,
        workflow_permanent_id="wpid_test",
    )

    assert [profile.name for profile in profiles] == ["managed active"]


@pytest.mark.asyncio
async def test_get_or_create_managed_browser_profile_returns_existing(repo_and_session) -> None:
    repo, _ = repo_and_session

    first, first_created = await repo.get_or_create_managed_browser_profile(
        organization_id=ORG,
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest="",
        name="Workflow (auto-saved session)",
    )
    second, second_created = await repo.get_or_create_managed_browser_profile(
        organization_id=ORG,
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest="",
        name="Workflow (auto-saved session)",
    )

    assert first.browser_profile_id == second.browser_profile_id
    assert first.is_managed is True
    assert first.workflow_permanent_id == "wpid_test"
    assert first_created is True
    assert second_created is False


class _CommitRaceSessionFactory:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self.session_factory = session_factory
        self.raised = False

    def __call__(self) -> Any:
        context = self.session_factory()
        factory = self

        class _Context:
            async def __aenter__(self) -> Any:
                session = await context.__aenter__()
                original_commit = session.commit

                async def commit() -> None:
                    if not factory.raised:
                        factory.raised = True
                        async with factory.session_factory() as winner_session:
                            winner = BrowserProfileModel(
                                browser_profile_id="bp_race_winner",
                                organization_id=ORG,
                                name="Race winner",
                                is_managed=True,
                                workflow_permanent_id="wpid_race",
                                browser_profile_key_digest="digest_race",
                                created_at=datetime(2026, 1, 1),
                                modified_at=datetime(2026, 1, 1),
                            )
                            winner_session.add(winner)
                            await winner_session.commit()
                        raise IntegrityError("insert", {}, Exception("duplicate"))
                    await original_commit()

                session.commit = commit
                return session

            async def __aexit__(self, *args: object) -> None:
                await context.__aexit__(*args)

        return _Context()


@pytest.mark.asyncio
async def test_get_or_create_managed_browser_profile_recovers_from_integrity_race(repo_and_session) -> None:
    _, session_factory = repo_and_session
    repo = BrowserSessionsRepository(_CommitRaceSessionFactory(session_factory))

    profile, created = await repo.get_or_create_managed_browser_profile(
        organization_id=ORG,
        workflow_permanent_id="wpid_race",
        browser_profile_key_digest="digest_race",
        name="Race loser",
    )

    assert profile.browser_profile_id == "bp_race_winner"
    assert created is False

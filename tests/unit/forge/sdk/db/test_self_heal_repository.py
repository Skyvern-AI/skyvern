"""Behavioral coverage for SelfHealRepository.

Runs against an in-memory SQLite DB so free-text sanitization is exercised for
real — a regression that stopped stripping NUL bytes before insert would raise
a Postgres error in production (see scripts.py/tasks.py's use of the same
sanitize_postgres_text helper) rather than being caught here.
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skyvern.forge.sdk.db.models import Base, HealEpisodeModel, WorkflowHealProposalModel
from skyvern.forge.sdk.db.repositories.self_heal import SelfHealRepository

ORG_A = "o_aaaaaaaaaaaaaaa"


@pytest_asyncio.fixture
async def repo_and_session() -> tuple:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[HealEpisodeModel.__table__, WorkflowHealProposalModel.__table__],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield SelfHealRepository(session_factory), session_factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_heal_episode_sanitizes_free_text_fields(repo_and_session) -> None:
    repo, _ = repo_and_session
    episode = await repo.create_heal_episode(
        organization_id=ORG_A,
        workflow_permanent_id="wpid_1",
        workflow_id="w_1",
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        block_label="block_1",
        engine="code",
        status="fired_failed",
        block_prompt="click the \x00submit button",
        block_code="print('hi')\x01\x00",
        failure_message="Timeout\x00Error: element not found",
    )
    assert "\x00" not in episode.block_prompt
    assert "\x00" not in episode.block_code
    assert "\x00" not in episode.failure_message
    assert episode.block_prompt == "click the submit button"
    assert episode.failure_message == "TimeoutError: element not found"


@pytest.mark.asyncio
async def test_create_heal_proposal_sanitizes_rendered_diff(repo_and_session) -> None:
    repo, _ = repo_and_session
    proposal = await repo.create_heal_proposal(
        organization_id=ORG_A,
        workflow_permanent_id="wpid_1",
        block_label="block_1",
        candidate_definition={"code": "print(1)"},
        episode_ids=["he_1"],
        base_version=1,
        base_definition_hash="hash_1",
        rendered_diff="-old\x00\n+new",
    )
    assert "\x00" not in proposal.rendered_diff
    assert proposal.rendered_diff == "-old\n+new"


@pytest.mark.asyncio
async def test_update_heal_proposal_status_compare_and_set(repo_and_session) -> None:
    repo, _ = repo_and_session
    proposal = await repo.create_heal_proposal(
        organization_id=ORG_A,
        workflow_permanent_id="wpid_1",
        block_label="block_1",
        candidate_definition={"code": "print(1)"},
        episode_ids=["he_1"],
        base_version=1,
        base_definition_hash="hash_1",
    )
    assert proposal.status == "proposed"

    stale_write = await repo.update_heal_proposal_status(
        heal_proposal_id=proposal.heal_proposal_id,
        organization_id=ORG_A,
        status="adopted",
        expected_current_status="stale",
    )
    assert stale_write is None

    adopted = await repo.update_heal_proposal_status(
        heal_proposal_id=proposal.heal_proposal_id,
        organization_id=ORG_A,
        status="adopted",
        expected_current_status="proposed",
    )
    assert adopted is not None
    assert adopted.status == "adopted"

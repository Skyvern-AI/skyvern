"""Behavioral coverage for SelfHealRepository.

Runs against an in-memory SQLite DB so free-text sanitization is exercised for
real — a regression that stopped stripping NUL bytes before insert would raise
a Postgres error in production (see scripts.py/tasks.py's use of the same
sanitize_postgres_text helper) rather than being caught here.
"""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skyvern.forge.sdk.db.models import Base, HealEpisodeModel, WorkflowHealProposalModel, WorkflowRunModel
from skyvern.forge.sdk.db.repositories.self_heal import SelfHealRepository

ORG_A = "o_aaaaaaaaaaaaaaa"
ORG_B = "o_bbbbbbbbbbbbbbb"


def _heal_episode_model(
    *,
    heal_episode_id: str | None = None,
    organization_id: str = ORG_A,
    workflow_permanent_id: str = "wpid_1",
    workflow_id: str = "w_1",
    workflow_run_id: str = "wr_1",
    workflow_run_block_id: str = "wrb_1",
    block_label: str = "block_1",
    status: str = "fired_failed",
    created_at: datetime,
) -> HealEpisodeModel:
    model_kwargs: dict[str, str | datetime] = {
        "organization_id": organization_id,
        "workflow_permanent_id": workflow_permanent_id,
        "workflow_id": workflow_id,
        "workflow_run_id": workflow_run_id,
        "workflow_run_block_id": workflow_run_block_id,
        "block_label": block_label,
        "engine": "code",
        "status": status,
        "created_at": created_at,
        "modified_at": created_at,
    }
    if heal_episode_id is not None:
        model_kwargs["heal_episode_id"] = heal_episode_id
    return HealEpisodeModel(**model_kwargs)


def _workflow_run_model(
    *,
    workflow_run_id: str,
    created_at: datetime,
    organization_id: str = ORG_A,
    workflow_permanent_id: str = "wpid_1",
    status: str = "completed",
    parent_workflow_run_id: str | None = None,
    copilot_session_id: str | None = None,
    debug_session_id: str | None = None,
) -> WorkflowRunModel:
    return WorkflowRunModel(
        workflow_run_id=workflow_run_id,
        workflow_id="wf_1",
        workflow_permanent_id=workflow_permanent_id,
        organization_id=organization_id,
        status=status,
        created_at=created_at,
        modified_at=created_at,
        parent_workflow_run_id=parent_workflow_run_id,
        copilot_session_id=copilot_session_id,
        debug_session_id=debug_session_id,
    )


@pytest_asyncio.fixture
async def repo_and_session() -> tuple:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[HealEpisodeModel.__table__, WorkflowHealProposalModel.__table__, WorkflowRunModel.__table__],
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


@pytest.mark.asyncio
async def test_get_heal_episodes_for_workflow_filters_and_paginates(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    now = datetime(2026, 1, 1, 0, 0, 0)
    async with session_factory() as session:
        session.add_all(
            [
                _heal_episode_model(
                    workflow_run_id="wr_target",
                    workflow_run_block_id="wrb_a",
                    block_label="block_x",
                    status="fired_failed",
                    created_at=now,
                ),
                _heal_episode_model(
                    workflow_run_id="wr_target",
                    workflow_run_block_id="wrb_b",
                    block_label="block_x",
                    status="fired_completed",
                    created_at=now + timedelta(minutes=1),
                ),
                _heal_episode_model(
                    workflow_run_id="wr_target",
                    workflow_run_block_id="wrb_c",
                    block_label="block_y",
                    status="fired_failed",
                    created_at=now + timedelta(minutes=2),
                ),
                _heal_episode_model(
                    workflow_permanent_id="wpid_other",
                    workflow_run_id="wr_target",
                    workflow_run_block_id="wrb_d",
                    block_label="block_x",
                    status="fired_failed",
                    created_at=now + timedelta(minutes=3),
                ),
                _heal_episode_model(
                    organization_id=ORG_B,
                    workflow_run_id="wr_target",
                    workflow_run_block_id="wrb_e",
                    block_label="block_x",
                    status="fired_failed",
                    created_at=now + timedelta(minutes=4),
                ),
            ]
        )
        await session.commit()

    all_for_workflow = await repo.get_heal_episodes_for_workflow(
        organization_id=ORG_A,
        workflow_permanent_id="wpid_1",
    )
    assert [episode.workflow_run_block_id for episode in all_for_workflow] == ["wrb_c", "wrb_b", "wrb_a"]

    block_filtered = await repo.get_heal_episodes_for_workflow(
        organization_id=ORG_A,
        workflow_permanent_id="wpid_1",
        block_label="block_x",
    )
    assert [episode.workflow_run_block_id for episode in block_filtered] == ["wrb_b", "wrb_a"]

    status_filtered = await repo.get_heal_episodes_for_workflow(
        organization_id=ORG_A,
        workflow_permanent_id="wpid_1",
        status="fired_failed",
    )
    assert [episode.workflow_run_block_id for episode in status_filtered] == ["wrb_c", "wrb_a"]

    paginated = await repo.get_heal_episodes_for_workflow(
        organization_id=ORG_A,
        workflow_permanent_id="wpid_1",
        limit=2,
        offset=1,
    )
    assert [episode.workflow_run_block_id for episode in paginated] == ["wrb_b", "wrb_a"]

    clamped = await repo.get_heal_episodes_for_workflow(
        organization_id=ORG_A,
        workflow_permanent_id="wpid_1",
        limit=-5,
    )
    assert [episode.workflow_run_block_id for episode in clamped] == ["wrb_c"]


@pytest.mark.asyncio
async def test_get_heal_episodes_for_run_returns_created_at_ascending(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    now = datetime(2026, 2, 1, 0, 0, 0)
    async with session_factory() as session:
        session.add_all(
            [
                _heal_episode_model(
                    workflow_run_id="wr_ordered",
                    workflow_run_block_id="wrb_late",
                    created_at=now + timedelta(minutes=2),
                ),
                _heal_episode_model(
                    workflow_run_id="wr_ordered",
                    workflow_run_block_id="wrb_early",
                    created_at=now,
                ),
                _heal_episode_model(
                    workflow_run_id="wr_ordered",
                    workflow_run_block_id="wrb_middle",
                    created_at=now + timedelta(minutes=1),
                ),
                _heal_episode_model(
                    workflow_run_id="wr_other",
                    workflow_run_block_id="wrb_other",
                    created_at=now + timedelta(minutes=3),
                ),
            ]
        )
        await session.commit()

    episodes = await repo.get_heal_episodes_for_run(organization_id=ORG_A, workflow_run_id="wr_ordered")
    assert [episode.workflow_run_block_id for episode in episodes] == ["wrb_early", "wrb_middle", "wrb_late"]


@pytest.mark.asyncio
async def test_get_heal_episodes_for_workflow_uses_heal_episode_id_tiebreaker(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    now = datetime(2026, 3, 1, 0, 0, 0)
    async with session_factory() as session:
        session.add_all(
            [
                _heal_episode_model(
                    heal_episode_id="he_001",
                    workflow_run_block_id="wrb_1",
                    created_at=now,
                ),
                _heal_episode_model(
                    heal_episode_id="he_003",
                    workflow_run_block_id="wrb_3",
                    created_at=now,
                ),
                _heal_episode_model(
                    heal_episode_id="he_002",
                    workflow_run_block_id="wrb_2",
                    created_at=now,
                ),
            ]
        )
        await session.commit()

    episodes = await repo.get_heal_episodes_for_workflow(
        organization_id=ORG_A,
        workflow_permanent_id="wpid_1",
    )
    assert [episode.heal_episode_id for episode in episodes] == ["he_003", "he_002", "he_001"]


@pytest.mark.asyncio
async def test_get_heal_episodes_for_run_uses_heal_episode_id_tiebreaker(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    now = datetime(2026, 4, 1, 0, 0, 0)
    async with session_factory() as session:
        session.add_all(
            [
                _heal_episode_model(
                    heal_episode_id="he_003",
                    workflow_run_id="wr_tie",
                    workflow_run_block_id="wrb_3",
                    created_at=now,
                ),
                _heal_episode_model(
                    heal_episode_id="he_001",
                    workflow_run_id="wr_tie",
                    workflow_run_block_id="wrb_1",
                    created_at=now,
                ),
                _heal_episode_model(
                    heal_episode_id="he_002",
                    workflow_run_id="wr_tie",
                    workflow_run_block_id="wrb_2",
                    created_at=now,
                ),
            ]
        )
        await session.commit()

    episodes = await repo.get_heal_episodes_for_run(organization_id=ORG_A, workflow_run_id="wr_tie")
    assert [episode.heal_episode_id for episode in episodes] == ["he_001", "he_002", "he_003"]


@pytest.mark.asyncio
async def test_get_recent_terminal_workflow_run_ids_filters_orders_and_limits(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    now = datetime(2026, 5, 1, 0, 0, 0)
    async with session_factory() as session:
        session.add_all(
            [
                _workflow_run_model(workflow_run_id="wr_old_completed", status="completed", created_at=now),
                _workflow_run_model(
                    workflow_run_id="wr_mid_terminated",
                    status="terminated",
                    created_at=now + timedelta(minutes=2),
                ),
                _workflow_run_model(
                    workflow_run_id="wr_new_failed", status="failed", created_at=now + timedelta(minutes=3)
                ),
                _workflow_run_model(
                    workflow_run_id="wr_newest_timed_out",
                    status="timed_out",
                    created_at=now + timedelta(minutes=4),
                ),
                _workflow_run_model(
                    workflow_run_id="wr_running", status="running", created_at=now + timedelta(minutes=5)
                ),
                _workflow_run_model(
                    workflow_run_id="wr_other_org",
                    organization_id=ORG_B,
                    status="failed",
                    created_at=now + timedelta(minutes=6),
                ),
                _workflow_run_model(
                    workflow_run_id="wr_other_workflow",
                    workflow_permanent_id="wpid_other",
                    status="failed",
                    created_at=now + timedelta(minutes=7),
                ),
                _workflow_run_model(
                    workflow_run_id="wr_child",
                    status="completed",
                    created_at=now + timedelta(minutes=8),
                    parent_workflow_run_id="wr_old_completed",
                ),
                _workflow_run_model(
                    workflow_run_id="wr_copilot",
                    status="completed",
                    created_at=now + timedelta(minutes=9),
                    copilot_session_id="cs_1",
                ),
                _workflow_run_model(
                    workflow_run_id="wr_debug",
                    status="completed",
                    created_at=now + timedelta(minutes=10),
                    debug_session_id="ds_1",
                ),
            ]
        )
        await session.commit()

    real_runs = await repo.get_recent_terminal_workflow_run_ids(
        organization_id=ORG_A,
        workflow_permanent_id="wpid_1",
        limit=50,
    )
    assert real_runs == ["wr_newest_timed_out", "wr_new_failed", "wr_mid_terminated", "wr_old_completed"]

    run_ids = await repo.get_recent_terminal_workflow_run_ids(
        organization_id=ORG_A,
        workflow_permanent_id="wpid_1",
        limit=3,
    )
    assert run_ids == ["wr_newest_timed_out", "wr_new_failed", "wr_mid_terminated"]

    bounded = await repo.get_recent_terminal_workflow_run_ids(
        organization_id=ORG_A,
        workflow_permanent_id="wpid_1",
        limit=0,
    )
    assert bounded == ["wr_newest_timed_out"]


@pytest.mark.asyncio
async def test_get_heal_episodes_for_runs_batches_and_scopes_to_org(repo_and_session) -> None:
    repo, session_factory = repo_and_session
    now = datetime(2026, 6, 1, 0, 0, 0)

    assert await repo.get_heal_episodes_for_runs(organization_id=ORG_A, workflow_run_ids=[]) == []

    async with session_factory() as session:
        session.add_all(
            [
                _heal_episode_model(
                    heal_episode_id="he_1",
                    workflow_run_id="wr_a",
                    workflow_run_block_id="wrb_a",
                    created_at=now + timedelta(minutes=2),
                ),
                _heal_episode_model(
                    heal_episode_id="he_2",
                    workflow_run_id="wr_b",
                    workflow_run_block_id="wrb_b",
                    created_at=now + timedelta(minutes=3),
                ),
                _heal_episode_model(
                    heal_episode_id="he_0",
                    workflow_run_id="wr_a",
                    workflow_run_block_id="wrb_c",
                    created_at=now + timedelta(minutes=1),
                ),
                _heal_episode_model(
                    heal_episode_id="he_other_org",
                    organization_id=ORG_B,
                    workflow_run_id="wr_a",
                    workflow_run_block_id="wrb_other",
                    created_at=now + timedelta(minutes=4),
                ),
                _heal_episode_model(
                    heal_episode_id="he_other_run",
                    workflow_run_id="wr_c",
                    workflow_run_block_id="wrb_other_run",
                    created_at=now + timedelta(minutes=5),
                ),
            ]
        )
        await session.commit()

    episodes = await repo.get_heal_episodes_for_runs(
        organization_id=ORG_A,
        workflow_run_ids=["wr_b", "wr_a"],
    )
    assert [episode.heal_episode_id for episode in episodes] == ["he_0", "he_1", "he_2"]

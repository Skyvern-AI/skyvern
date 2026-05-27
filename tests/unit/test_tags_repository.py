"""TagsRepository unit tests against an in-memory SQLite database."""

from __future__ import annotations

from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import (
    Base,
    TagKeyModel,
    WorkflowTagEventModel,
)
from skyvern.forge.sdk.db.repositories.tags import (
    MAX_TAGS_PER_WORKFLOW,
    TagCountLimitExceeded,
    TagsRepository,
)
from skyvern.forge.sdk.workflow.models.tags import (
    CallerType,
    TagEventType,
    TagSource,
    TagWriteContext,
)

ORG_ID = "o_test"
WPID = "wpid_alpha"


async def _enable_partial_unique_indexes(engine: AsyncEngine) -> None:
    """SQLite ignores postgresql_where, so the model's partial unique indexes
    become full unique indexes. Drop them and re-create with WHERE clauses so
    test behavior matches Postgres semantics.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP INDEX IF EXISTS workflow_tag_events_active_set_unique")
        await conn.exec_driver_sql(
            "CREATE UNIQUE INDEX workflow_tag_events_active_set_unique "
            "ON workflow_tag_events (organization_id, workflow_permanent_id, key) "
            "WHERE superseded_at IS NULL AND event_type = 'set'"
        )
        await conn.exec_driver_sql("DROP INDEX IF EXISTS ix_tag_keys_org_key_active")
        await conn.exec_driver_sql(
            "CREATE UNIQUE INDEX ix_tag_keys_org_key_active ON tag_keys (organization_id, key) WHERE deleted_at IS NULL"
        )


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _enable_partial_unique_indexes(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def repo(engine: AsyncEngine) -> TagsRepository:
    db = BaseAlchemyDB(engine)
    return TagsRepository(db.Session, debug_enabled=False)


async def _all_events(repo: TagsRepository) -> list[WorkflowTagEventModel]:
    async with repo.Session() as session:
        result = await session.execute(
            select(WorkflowTagEventModel).order_by(WorkflowTagEventModel.set_at, WorkflowTagEventModel.tag_event_id)
        )
        return list(result.scalars().all())


async def _registered_keys(repo: TagsRepository) -> list[str]:
    async with repo.Session() as session:
        result = await session.execute(select(TagKeyModel.key).order_by(TagKeyModel.key))
        return list(result.scalars().all())


async def _event_count(repo: TagsRepository) -> int:
    async with repo.Session() as session:
        return (await session.execute(select(func.count(WorkflowTagEventModel.tag_event_id)))).scalar_one()


def _ctx(caller_id: str = "user_abc") -> TagWriteContext:
    return TagWriteContext(
        caller_id=caller_id,
        source=TagSource.MANUAL,
        caller_type=CallerType.USER,
    )


@pytest.mark.asyncio
async def test_no_op_when_sets_and_deletes_empty(repo: TagsRepository) -> None:
    changes = await repo.apply_tag_changes(WPID, ORG_ID, sets={}, deletes=set(), context=_ctx())
    assert changes == []


@pytest.mark.asyncio
async def test_initial_set_creates_event_and_registers_key(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())

    assert await repo.get_active_tags_for_workflow(WPID, ORG_ID) == {"env": "prod"}
    assert await _registered_keys(repo) == ["env"]


@pytest.mark.asyncio
async def test_set_existing_key_supersedes_prior_row(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx())

    assert await repo.get_active_tags_for_workflow(WPID, ORG_ID) == {"env": "stg"}

    rows = await _all_events(repo)
    assert len(rows) == 2
    assert rows[0].value == "prod"
    assert rows[0].superseded_at is not None
    assert rows[1].value == "stg"
    assert rows[1].superseded_at is None


@pytest.mark.asyncio
async def test_set_with_same_value_is_no_op(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    changes = await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())

    assert changes == []
    assert await _event_count(repo) == 1


@pytest.mark.asyncio
async def test_delete_writes_delete_event_with_null_value(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes(WPID, ORG_ID, sets={}, deletes={"env"}, context=_ctx())

    assert await repo.get_active_tags_for_workflow(WPID, ORG_ID) == {}

    rows = await _all_events(repo)
    assert len(rows) == 2
    assert rows[0].event_type == TagEventType.SET.value
    assert rows[0].superseded_at is not None
    assert rows[1].event_type == TagEventType.DELETE.value
    assert rows[1].value is None


@pytest.mark.asyncio
async def test_delete_of_absent_key_is_no_op(repo: TagsRepository) -> None:
    changes = await repo.apply_tag_changes(WPID, ORG_ID, sets={}, deletes={"never_set"}, context=_ctx())
    assert changes == []


@pytest.mark.asyncio
async def test_sets_win_on_same_key_collision(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "stg"}, deletes={"env"}, context=_ctx())

    assert await repo.get_active_tags_for_workflow(WPID, ORG_ID) == {"env": "stg"}
    rows = await _all_events(repo)
    # No DELETE event got emitted for the colliding key.
    assert [r.event_type for r in rows] == [TagEventType.SET.value, TagEventType.SET.value]


@pytest.mark.asyncio
async def test_per_workflow_cap_enforced(repo: TagsRepository) -> None:
    tags = {f"k{i}": f"v{i}" for i in range(MAX_TAGS_PER_WORKFLOW)}
    await repo.apply_tag_changes(WPID, ORG_ID, sets=tags, deletes=set(), context=_ctx())

    with pytest.raises(TagCountLimitExceeded):
        await repo.apply_tag_changes(WPID, ORG_ID, sets={"one_too_many": "v"}, deletes=set(), context=_ctx())


@pytest.mark.asyncio
async def test_cap_allows_replace_at_limit(repo: TagsRepository) -> None:
    tags = {f"k{i}": f"v{i}" for i in range(MAX_TAGS_PER_WORKFLOW)}
    await repo.apply_tag_changes(WPID, ORG_ID, sets=tags, deletes=set(), context=_ctx())

    await repo.apply_tag_changes(WPID, ORG_ID, sets={"k0": "new"}, deletes=set(), context=_ctx())

    current = await repo.get_active_tags_for_workflow(WPID, ORG_ID)
    assert current["k0"] == "new"
    assert len(current) == MAX_TAGS_PER_WORKFLOW


@pytest.mark.asyncio
async def test_history_returns_superseded_and_delete_events(
    repo: TagsRepository,
) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes(WPID, ORG_ID, sets={}, deletes={"env"}, context=_ctx())

    history = await repo.get_tag_event_history(WPID, ORG_ID)
    assert len(history) == 3
    types = [row.event_type for row in history]
    assert types[0] == TagEventType.DELETE.value


@pytest.mark.asyncio
async def test_attribution_carries_caller_and_source(repo: TagsRepository) -> None:
    ctx = TagWriteContext(
        caller_id="user_42",
        source=TagSource.BULK_APPLY,
        caller_type=CallerType.API_KEY,
    )
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"team": "core"}, deletes=set(), context=ctx)

    history = await repo.get_tag_event_history(WPID, ORG_ID)
    assert history[0].set_by == "user_42"
    assert history[0].source == TagSource.BULK_APPLY.value
    assert history[0].caller_type == CallerType.API_KEY.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller_type",
    [CallerType.USER, CallerType.API_KEY, CallerType.SYSTEM],
    ids=["user", "api_key", "system"],
)
async def test_caller_type_persists_for_every_value(repo: TagsRepository, caller_type: CallerType) -> None:
    """The tag audit row records whether the writer was a person, an API key,
    or a system actor. Lets downstream queries ("show me API-driven tag
    activity this week") run as a column predicate.
    """
    ctx = TagWriteContext(
        caller_id="actor",
        source=TagSource.MANUAL,
        caller_type=caller_type,
    )
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"team": "core"}, deletes=set(), context=ctx)
    history = await repo.get_tag_event_history(WPID, ORG_ID)
    assert history[0].caller_type == caller_type.value


@pytest.mark.asyncio
async def test_caller_type_persists_as_null_when_unset(repo: TagsRepository) -> None:
    """Backfill scripts that lack a request context can omit caller_type;
    the column is nullable so the audit log remains writable.
    """
    ctx = TagWriteContext(caller_id="backfill_script", source=TagSource.BACKFILL)
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"team": "core"}, deletes=set(), context=ctx)
    history = await repo.get_tag_event_history(WPID, ORG_ID)
    assert history[0].caller_type is None


@pytest.mark.asyncio
async def test_isolation_between_workflows_in_same_org(repo: TagsRepository) -> None:
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_b", ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx())

    assert await repo.get_active_tags_for_workflow("wpid_a", ORG_ID) == {"env": "prod"}
    assert await repo.get_active_tags_for_workflow("wpid_b", ORG_ID) == {"env": "stg"}


def test_tag_count_limit_is_registered_as_passthrough_exception() -> None:
    """Cap breaches must log as BusinessLogicError (WARN), not
    UnexpectedError (ERROR). Mirrors ScheduleLimitExceededError.
    """
    from skyvern.forge.sdk.db import _error_handling

    assert TagCountLimitExceeded in _error_handling._PASSTHROUGH_EXCEPTIONS


# ---------------------------------------------------------------------------
# Realistic tag examples gallery
#
# Doubles as documentation for what tags look like in practice. Mirrors the
# "Tag examples gallery" table in the Notion PRD's Design section. If you
# change a row here, change the PRD too.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tags,description",
    [
        pytest.param(
            {"team": "payments"},
            "single-dimension ownership",
            id="team",
        ),
        pytest.param(
            {"environment": "prod"},
            "lifecycle stage",
            id="environment",
        ),
        pytest.param(
            {"customer": "acme-corp"},
            "external customer this workflow serves",
            id="customer",
        ),
        pytest.param(
            {"team": "payments", "environment": "prod"},
            "two orthogonal dimensions on one workflow",
            id="team-and-environment",
        ),
        pytest.param(
            {
                "team": "payments",
                "environment": "prod",
                "customer": "acme-corp",
                "cost_center": "R&D",
                "priority": "p0",
            },
            "five-axis tagging — what a fully-categorized workflow looks like",
            id="five-axis",
        ),
        pytest.param(
            {"use_case": "form-fill", "team": "platform"},
            "workflow archetype + ownership",
            id="archetype-plus-team",
        ),
    ],
)
async def test_realistic_tag_examples(repo: TagsRepository, tags: dict[str, str], description: str) -> None:
    """Each row is a realistic tagging pattern a customer might use.

    Reading these tests is the fastest way to learn what tags are for and
    what shapes the system supports.
    """
    await repo.apply_tag_changes(WPID, ORG_ID, sets=tags, deletes=set(), context=_ctx())
    assert await repo.get_active_tags_for_workflow(WPID, ORG_ID) == tags


@pytest.mark.asyncio
async def test_dotted_key_hierarchy_works_without_schema_change(repo: TagsRepository) -> None:
    """Dotted keys (`team.payments.billing`) are the canonical lightweight
    hierarchy convention. The key regex already allows `.`, so deep nesting
    is expressible today without any schema work.

    Parent and child keys live in the same flat namespace; a workflow can
    carry both at once.
    """
    await repo.apply_tag_changes(
        WPID,
        ORG_ID,
        sets={
            "team": "payments",  # parent dimension
            "team.subteam": "billing",  # nested child as a separate key
            "team.subteam.squad": "checkout",  # arbitrary depth
        },
        deletes=set(),
        context=_ctx(),
    )
    active = await repo.get_active_tags_for_workflow(WPID, ORG_ID)
    assert active == {
        "team": "payments",
        "team.subteam": "billing",
        "team.subteam.squad": "checkout",
    }


@pytest.mark.asyncio
async def test_same_key_value_across_many_workflows_n_to_m(repo: TagsRepository) -> None:
    """The data model is N:M: one (key, value) like `team:payments` can
    apply across many workflows, AND a single workflow can carry many keys.
    There is no `tags` join table — the (org, key, value) triple in the
    event log IS the tag identity.
    """
    # Three workflows in the same org, all tagged team:payments
    for wpid in ("wpid_invoice_processor", "wpid_refund_handler", "wpid_billing_sync"):
        await repo.apply_tag_changes(wpid, ORG_ID, sets={"team": "payments"}, deletes=set(), context=_ctx())

    # And one of them has additional tags — orthogonal dimensions stack freely
    await repo.apply_tag_changes(
        "wpid_invoice_processor",
        ORG_ID,
        sets={"environment": "prod", "priority": "p0"},
        deletes=set(),
        context=_ctx(),
    )

    assert await repo.get_active_tags_for_workflow("wpid_invoice_processor", ORG_ID) == {
        "team": "payments",
        "environment": "prod",
        "priority": "p0",
    }
    assert await repo.get_active_tags_for_workflow("wpid_refund_handler", ORG_ID) == {
        "team": "payments",
    }
    assert await repo.get_active_tags_for_workflow("wpid_billing_sync", ORG_ID) == {
        "team": "payments",
    }


@pytest.mark.asyncio
async def test_value_can_contain_colons(repo: TagsRepository) -> None:
    """Colons inside values are data, not delimiters. URLs, timestamps,
    and external IDs commonly contain colons — they're stored verbatim.

    (The comma-as-pair-separator rule lives in the URL parser, not the
    repository; the repository accepts any string value < 256 chars.)
    """
    await repo.apply_tag_changes(
        WPID,
        ORG_ID,
        sets={
            "jira_ticket": "PROJ-1234:bugfix",
            "captured_at": "2026-05-25T18:30:00Z",
        },
        deletes=set(),
        context=_ctx(),
    )
    assert await repo.get_active_tags_for_workflow(WPID, ORG_ID) == {
        "jira_ticket": "PROJ-1234:bugfix",
        "captured_at": "2026-05-25T18:30:00Z",
    }

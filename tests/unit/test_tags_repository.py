"""TagsRepository unit tests against an in-memory SQLite database."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import (
    Base,
    TagKeyModel,
    TagValueModel,
    WorkflowModel,
    WorkflowRunModel,
    WorkflowRunTagEventModel,
    WorkflowTagEventModel,
)
from skyvern.forge.sdk.db.repositories.tags import (
    MAX_TAGS_PER_WORKFLOW,
    RunTagWorkflowRunMismatch,
    TagCountLimitExceeded,
    TagsRepository,
    TagValueRenameCollision,
)
from skyvern.forge.sdk.workflow.models.tags import (
    CallerType,
    TagEventType,
    TagSource,
    TagWriteContext,
)
from skyvern.forge.sdk.workflow.models.validators import TAG_COLOR_PALETTE

ORG_ID = "o_test"
WPID = "wpid_alpha"
WRID = "wr_alpha"


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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


async def _all_run_events(repo: TagsRepository) -> list[WorkflowRunTagEventModel]:
    async with repo.Session() as session:
        result = await session.execute(
            select(WorkflowRunTagEventModel).order_by(
                WorkflowRunTagEventModel.set_at, WorkflowRunTagEventModel.tag_event_id
            )
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


async def _seed_workflow(repo: TagsRepository, wpid: str, *, org_id: str = ORG_ID, deleted: bool = False) -> None:
    """Insert a WorkflowModel row so the non-deleted-workflow filter on the value
    counts can resolve a workflow (a tag only exists on a real workflow). Pass
    ``deleted=True`` to seed a soft-deleted workflow."""
    async with repo.Session() as session:
        session.add(
            WorkflowModel(
                organization_id=org_id,
                workflow_permanent_id=wpid,
                title="t",
                workflow_definition={},
                deleted_at=datetime.now(timezone.utc) if deleted else None,
            )
        )
        await session.commit()


async def _seed_workflow_run(repo: TagsRepository, wrid: str, *, org_id: str = ORG_ID) -> None:
    async with repo.Session() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id=wrid,
                workflow_id=f"w_{wrid}",
                workflow_permanent_id=f"wpid_{wrid}",
                organization_id=org_id,
                status="completed",
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_no_op_when_sets_and_deletes_empty(repo: TagsRepository) -> None:
    changes = await repo.apply_tag_changes(WPID, ORG_ID, sets={}, deletes=set(), context=_ctx())
    assert changes == []


@pytest.mark.asyncio
async def test_initial_set_creates_event_and_registers_key(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())

    assert await repo.get_active_grouped_tags_for_workflow(WPID, ORG_ID) == {"env": "prod"}
    assert await _registered_keys(repo) == ["env"]


@pytest.mark.asyncio
async def test_set_existing_key_supersedes_prior_row(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx())

    assert await repo.get_active_grouped_tags_for_workflow(WPID, ORG_ID) == {"env": "stg"}

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

    assert await repo.get_active_grouped_tags_for_workflow(WPID, ORG_ID) == {}

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

    assert await repo.get_active_grouped_tags_for_workflow(WPID, ORG_ID) == {"env": "stg"}
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

    current = await repo.get_active_grouped_tags_for_workflow(WPID, ORG_ID)
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

    assert await repo.get_active_grouped_tags_for_workflow("wpid_a", ORG_ID) == {"env": "prod"}
    assert await repo.get_active_grouped_tags_for_workflow("wpid_b", ORG_ID) == {"env": "stg"}


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
    assert await repo.get_active_grouped_tags_for_workflow(WPID, ORG_ID) == tags


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
    active = await repo.get_active_grouped_tags_for_workflow(WPID, ORG_ID)
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

    assert await repo.get_active_grouped_tags_for_workflow("wpid_invoice_processor", ORG_ID) == {
        "team": "payments",
        "environment": "prod",
        "priority": "p0",
    }
    assert await repo.get_active_grouped_tags_for_workflow("wpid_refund_handler", ORG_ID) == {
        "team": "payments",
    }
    assert await repo.get_active_grouped_tags_for_workflow("wpid_billing_sync", ORG_ID) == {
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
    assert await repo.get_active_grouped_tags_for_workflow(WPID, ORG_ID) == {
        "jira_ticket": "PROJ-1234:bugfix",
        "captured_at": "2026-05-25T18:30:00Z",
    }


# ---------------------------- Phase 3 read helpers ----------------------------


@pytest.mark.asyncio
async def test_get_active_tags_for_workflows_batch(repo: TagsRepository) -> None:
    """Batch read groups rows by wpid and applies the org filter."""
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_beta", ORG_ID, sets={"team": "growth"}, deletes=set(), context=_ctx())
    # Different org for the same wpid — must not bleed across.
    await repo.apply_tag_changes(WPID, "o_other", sets={"secret": "leak"}, deletes=set(), context=_ctx("other"))

    result = await repo.get_active_tags_for_workflows([WPID, "wpid_beta", "wpid_missing"], ORG_ID)
    assert result == {WPID: [("env", "prod")], "wpid_beta": [("team", "growth")]}


@pytest.mark.asyncio
async def test_get_active_tags_for_workflows_empty_input(repo: TagsRepository) -> None:
    assert await repo.get_active_tags_for_workflows([], ORG_ID) == {}


@pytest.mark.asyncio
async def test_apply_run_tag_changes_set_supersede_delete_and_history_key_filter(repo: TagsRepository) -> None:
    await _seed_workflow_run(repo, WRID)

    await repo.apply_run_tag_changes(WRID, ORG_ID, sets={"env": "prod", "team": "core"}, deletes=set(), context=_ctx())
    await repo.apply_run_tag_changes(WRID, ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx())
    await repo.apply_run_tag_changes(WRID, ORG_ID, sets={}, deletes={"team"}, context=_ctx())

    assert await repo.get_active_grouped_tags_for_run(WRID, ORG_ID) == {"env": "stg"}

    history = await repo.get_run_tag_event_history(WRID, ORG_ID, key="env")
    assert [(event.event_type, event.key, event.value) for event in history] == [
        (TagEventType.SET.value, "env", "stg"),
        (TagEventType.SET.value, "env", "prod"),
    ]
    assert history[1].superseded_at is not None


@pytest.mark.asyncio
async def test_apply_run_tag_changes_supports_standalone_labels_and_cap(repo: TagsRepository) -> None:
    await _seed_workflow_run(repo, WRID)

    grouped = {f"k{i}": "v" for i in range(MAX_TAGS_PER_WORKFLOW - 1)}
    await repo.apply_run_tag_changes(
        WRID, ORG_ID, sets=grouped, deletes=set(), context=_ctx(), label_sets=["one_label"]
    )

    rows = await repo.get_active_tag_events_for_run(WRID, ORG_ID)
    assert {row.value for row in rows if row.key is None} == {"one_label"}

    with pytest.raises(TagCountLimitExceeded):
        await repo.apply_run_tag_changes(WRID, ORG_ID, sets={}, deletes=set(), context=_ctx(), label_sets=["over"])


@pytest.mark.asyncio
async def test_apply_run_tag_changes_rejects_run_from_other_org(repo: TagsRepository) -> None:
    await _seed_workflow_run(repo, WRID, org_id=ORG_ID)

    with pytest.raises(RunTagWorkflowRunMismatch):
        await repo.apply_run_tag_changes(WRID, "o_other", sets={"env": "prod"}, deletes=set(), context=_ctx())

    assert await repo.get_active_grouped_tags_for_run(WRID, "o_other") == {}


@pytest.mark.asyncio
async def test_get_active_tags_for_runs_batch(repo: TagsRepository) -> None:
    await _seed_workflow_run(repo, WRID)
    await _seed_workflow_run(repo, "wr_beta")
    await _seed_workflow_run(repo, "wr_other", org_id="o_other")

    await repo.apply_run_tag_changes(
        WRID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), label_sets=["urgent"]
    )
    await repo.apply_run_tag_changes("wr_beta", ORG_ID, sets={"team": "growth"}, deletes=set(), context=_ctx())
    await repo.apply_run_tag_changes(
        "wr_other", "o_other", sets={"secret": "leak"}, deletes=set(), context=_ctx("other")
    )

    result = await repo.get_active_tags_for_runs([WRID, "wr_beta", "wr_missing"], ORG_ID)
    assert set(result[WRID]) == {("env", "prod"), (None, "urgent")}
    assert result["wr_beta"] == [("team", "growth")]
    assert "wr_missing" not in result


@pytest.mark.asyncio
async def test_get_active_tags_for_runs_empty_input(repo: TagsRepository) -> None:
    assert await repo.get_active_tags_for_runs([], ORG_ID) == {}


@pytest.mark.asyncio
async def test_count_active_runs_per_key_and_value(repo: TagsRepository) -> None:
    for wrid in ("wr_a", "wr_b", "wr_c", "wr_d"):
        await _seed_workflow_run(repo, wrid)

    await repo.apply_run_tag_changes("wr_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_run_tag_changes(
        "wr_b", ORG_ID, sets={"env": "prod", "team": "core"}, deletes=set(), context=_ctx()
    )
    await repo.apply_run_tag_changes("wr_c", ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx())
    await repo.apply_run_tag_changes("wr_c", ORG_ID, sets={}, deletes={"env"}, context=_ctx())
    await repo.apply_run_tag_changes("wr_d", ORG_ID, sets={}, deletes=set(), context=_ctx(), label_sets=["standalone"])

    assert await repo.count_active_runs_per_key(ORG_ID) == {"env": 2, "team": 1}
    assert await repo.count_active_runs_per_value(ORG_ID) == {("env", "prod"): 2, ("team", "core"): 1}


@pytest.mark.asyncio
async def test_list_tag_keys_orders_alphabetically(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"zeta": "1", "alpha": "2", "mu": "3"}, deletes=set(), context=_ctx()
    )
    rows = await repo.list_tag_keys(ORG_ID)
    assert [r.key for r in rows] == ["alpha", "mu", "zeta"]


@pytest.mark.asyncio
async def test_list_tag_keys_excludes_other_orgs(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"a": "1"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes(WPID, "o_other", sets={"b": "2"}, deletes=set(), context=_ctx("other"))
    rows = await repo.list_tag_keys(ORG_ID)
    assert [r.key for r in rows] == ["a"]


@pytest.mark.asyncio
async def test_update_tag_key_description_sets_value(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    row = await repo.update_tag_key_description(ORG_ID, "env", "deployment environment")
    assert row is not None
    assert row.description == "deployment environment"


@pytest.mark.asyncio
async def test_update_tag_key_description_unknown_returns_none(repo: TagsRepository) -> None:
    row = await repo.update_tag_key_description(ORG_ID, "never_seen", "x")
    assert row is None


@pytest.mark.asyncio
async def test_update_tag_key_description_org_scoped(repo: TagsRepository) -> None:
    """Updating in one org must not match a same-keyed row in another org."""
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    row = await repo.update_tag_key_description("o_other", "env", "shouldn't find")
    assert row is None


@pytest.mark.asyncio
async def test_get_tag_event_history_filters_by_since(repo: TagsRepository) -> None:
    """`since` is inclusive; events earlier than the cutoff are excluded."""
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    cutoff = datetime.now(timezone.utc)
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "stage"}, deletes=set(), context=_ctx())

    events = await repo.get_tag_event_history(WPID, ORG_ID, since=cutoff)
    # Only the second SET is at-or-after cutoff.
    assert len(events) == 1
    assert events[0].value == "stage"


@pytest.mark.asyncio
async def test_get_tag_event_history_filters_by_key(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod", "team": "growth"}, deletes=set(), context=_ctx())
    events = await repo.get_tag_event_history(WPID, ORG_ID, key="team")
    assert {e.key for e in events} == {"team"}


# ---------------------------- Race-safe TagKeyModel registration (RISK-2) ----------------------------


@pytest.mark.asyncio
async def test_apply_tag_changes_idempotent_when_tag_key_already_exists(repo: TagsRepository) -> None:
    """A concurrent first-use writer that beat us to registering the TagKey
    must not surface IntegrityError. Simulated by pre-inserting the row out
    of band, then running apply_tag_changes for the same key — the
    ON CONFLICT DO NOTHING path swallows the conflict."""
    async with repo.Session() as session:
        session.add(TagKeyModel(organization_id=ORG_ID, key="env"))
        await session.commit()

    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())

    # Still exactly one TagKeyModel row — the duplicate insert was swallowed.
    async with repo.Session() as session:
        keys = (
            (
                await session.execute(
                    select(TagKeyModel).where(TagKeyModel.organization_id == ORG_ID).where(TagKeyModel.key == "env")
                )
            )
            .scalars()
            .all()
        )
    assert len(keys) == 1
    assert await repo.get_active_grouped_tags_for_workflow(WPID, ORG_ID) == {"env": "prod"}


@pytest.mark.asyncio
async def test_apply_tag_changes_registers_new_key_when_absent(repo: TagsRepository) -> None:
    """Same code path, normal case: no existing TagKey row, INSERT proceeds
    and the registry gets the new entry."""
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    assert await _registered_keys(repo) == ["env"]


@pytest.mark.asyncio
async def test_count_active_workflows_per_key(repo: TagsRepository) -> None:
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_b", ORG_ID, sets={"env": "stg", "team": "core"}, deletes=set(), context=_ctx())

    counts = await repo.count_active_workflows_per_key(ORG_ID)
    assert counts == {"env": 2, "team": 1}


@pytest.mark.asyncio
async def test_count_active_workflows_per_key_ignores_deleted_tags(repo: TagsRepository) -> None:
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={}, deletes={"env"}, context=_ctx())

    assert await repo.count_active_workflows_per_key(ORG_ID) == {}


@pytest.mark.asyncio
async def test_delete_tag_key_cascades_across_workflows(repo: TagsRepository) -> None:
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_b", ORG_ID, sets={"env": "stg", "team": "core"}, deletes=set(), context=_ctx())

    removed = await repo.delete_tag_key(ORG_ID, "env", _ctx())

    assert removed == 2
    # Tag gone from both workflows...
    assert await repo.get_active_grouped_tags_for_workflow("wpid_a", ORG_ID) == {}
    assert await repo.get_active_grouped_tags_for_workflow("wpid_b", ORG_ID) == {"team": "core"}
    # ...and the key no longer appears in the active registry.
    assert [row.key for row in await repo.list_tag_keys(ORG_ID)] == ["team"]


@pytest.mark.asyncio
async def test_delete_tag_key_cascades_across_workflow_runs(repo: TagsRepository) -> None:
    await _seed_workflow_run(repo, "wr_a")
    await _seed_workflow_run(repo, "wr_b")

    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_run_tag_changes("wr_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_run_tag_changes("wr_b", ORG_ID, sets={"env": "stg", "team": "core"}, deletes=set(), context=_ctx())

    removed_workflows = await repo.delete_tag_key(ORG_ID, "env", _ctx())

    assert removed_workflows == 1
    assert await repo.get_active_grouped_tags_for_workflow("wpid_a", ORG_ID) == {}
    assert await repo.get_active_grouped_tags_for_run("wr_a", ORG_ID) == {}
    assert await repo.get_active_grouped_tags_for_run("wr_b", ORG_ID) == {"team": "core"}
    rows = await _all_run_events(repo)
    run_deletes = [row for row in rows if row.event_type == TagEventType.DELETE.value]
    assert {(row.workflow_run_id, row.key, row.value) for row in run_deletes} == {
        ("wr_a", "env", None),
        ("wr_b", "env", None),
    }


@pytest.mark.asyncio
async def test_delete_tag_key_unknown_returns_none(repo: TagsRepository) -> None:
    assert await repo.delete_tag_key(ORG_ID, "never_seen", _ctx()) is None


@pytest.mark.asyncio
async def test_delete_tag_key_idempotent(repo: TagsRepository) -> None:
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())

    assert await repo.delete_tag_key(ORG_ID, "env", _ctx()) == 1
    # Second call: key already soft-deleted, no active SETs left.
    assert await repo.delete_tag_key(ORG_ID, "env", _ctx()) is None


@pytest.mark.asyncio
async def test_delete_tag_key_then_reapply_reregisters(repo: TagsRepository) -> None:
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.delete_tag_key(ORG_ID, "env", _ctx())

    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "dev"}, deletes=set(), context=_ctx())

    assert await repo.get_active_grouped_tags_for_workflow("wpid_a", ORG_ID) == {"env": "dev"}
    assert [row.key for row in await repo.list_tag_keys(ORG_ID)] == ["env"]


# ---------------------- Phase 6.1: key-optional (standalone) labels ----------------------


async def _active_labels(repo: TagsRepository, wpid: str = WPID, org_id: str = ORG_ID) -> set[str]:
    """Standalone labels (no group) currently active on a workflow."""
    rows = await repo.get_active_tag_events_for_workflow(wpid, org_id)
    return {row.value for row in rows if row.key is None}


@pytest.mark.asyncio
async def test_standalone_label_set_and_read(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={}, deletes=set(), context=_ctx(), label_sets=["production"])

    # Standalone labels carry no key, so they don't show in the grouped map...
    assert await repo.get_active_grouped_tags_for_workflow(WPID, ORG_ID) == {}
    # ...and they don't register a tag key.
    assert await _registered_keys(repo) == []
    # But the label is active.
    assert await _active_labels(repo) == {"production"}


@pytest.mark.asyncio
async def test_standalone_label_re_add_is_no_op(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={}, deletes=set(), context=_ctx(), label_sets=["urgent"])
    changes = await repo.apply_tag_changes(WPID, ORG_ID, sets={}, deletes=set(), context=_ctx(), label_sets=["urgent"])
    assert changes == []
    assert await _event_count(repo) == 1


@pytest.mark.asyncio
async def test_standalone_and_grouped_coexist(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), label_sets=["urgent"]
    )
    assert await repo.get_active_grouped_tags_for_workflow(WPID, ORG_ID) == {"env": "prod"}
    assert await _active_labels(repo) == {"urgent"}


@pytest.mark.asyncio
async def test_same_value_grouped_and_standalone_are_independent(repo: TagsRepository) -> None:
    """A grouped tag `env:prod` and a standalone label `prod` are different
    identities (key vs value) and coexist on one workflow."""
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), label_sets=["prod"])
    assert await repo.get_active_grouped_tags_for_workflow(WPID, ORG_ID) == {"env": "prod"}
    assert await _active_labels(repo) == {"prod"}


@pytest.mark.asyncio
async def test_standalone_label_delete_records_value(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={}, deletes=set(), context=_ctx(), label_sets=["production"])
    await repo.apply_tag_changes(WPID, ORG_ID, sets={}, deletes=set(), context=_ctx(), label_deletes=["production"])

    assert await _active_labels(repo) == set()
    rows = await _all_events(repo)
    assert len(rows) == 2
    assert rows[1].event_type == TagEventType.DELETE.value
    # The standalone-label delete records which label was removed (no key to identify it).
    assert rows[1].key is None
    assert rows[1].value == "production"


@pytest.mark.asyncio
async def test_standalone_label_set_wins_over_delete(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={}, deletes=set(), context=_ctx(), label_sets=["keep"])
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={}, deletes=set(), context=_ctx(), label_sets=["keep"], label_deletes=["keep"]
    )
    # Set wins: the label survives and no DELETE event was written.
    assert await _active_labels(repo) == {"keep"}
    assert [r.event_type for r in await _all_events(repo)] == [TagEventType.SET.value]


@pytest.mark.asyncio
async def test_cap_counts_grouped_and_labels_together(repo: TagsRepository) -> None:
    grouped = {f"k{i}": "v" for i in range(MAX_TAGS_PER_WORKFLOW - 1)}
    await repo.apply_tag_changes(WPID, ORG_ID, sets=grouped, deletes=set(), context=_ctx(), label_sets=["one_label"])
    # At the cap (19 grouped + 1 label = 20); one more label trips it.
    with pytest.raises(TagCountLimitExceeded):
        await repo.apply_tag_changes(WPID, ORG_ID, sets={}, deletes=set(), context=_ctx(), label_sets=["over"])


@pytest.mark.asyncio
async def test_count_active_workflows_per_key_ignores_standalone(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), label_sets=["urgent"]
    )
    # Standalone labels have no key and must not appear in the per-key counts.
    assert await repo.count_active_workflows_per_key(ORG_ID) == {"env": 1}


@pytest.mark.asyncio
async def test_batch_includes_standalone_labels(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), label_sets=["urgent"]
    )
    result = await repo.get_active_tags_for_workflows([WPID], ORG_ID)
    assert set(result[WPID]) == {("env", "prod"), (None, "urgent")}


async def _tag_value_rows(repo: TagsRepository) -> list[TagValueModel]:
    async with repo.Session() as session:
        result = await session.execute(
            select(TagValueModel)
            .where(TagValueModel.deleted_at.is_(None))
            .order_by(TagValueModel.key, TagValueModel.value)
        )
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_set_without_color_registers_random_palette_color(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())

    rows = await _tag_value_rows(repo)
    assert len(rows) == 1
    assert (rows[0].key, rows[0].value) == ("env", "prod")
    assert rows[0].color in TAG_COLOR_PALETTE


@pytest.mark.asyncio
async def test_set_with_color_persists_provided_color(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "blue"}
    )

    rows = await _tag_value_rows(repo)
    assert len(rows) == 1
    assert rows[0].color == "blue"


@pytest.mark.asyncio
async def test_tag_value_color_is_idempotent_without_color(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "teal"}
    )
    # Re-SET the same (key, value) with no color: the existing color must be kept,
    # and no duplicate registry row may appear under the partial unique.
    await repo.apply_tag_changes("wpid_other", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())

    rows = await _tag_value_rows(repo)
    assert len(rows) == 1
    assert rows[0].color == "teal"


@pytest.mark.asyncio
async def test_tag_value_color_override_updates_existing(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "blue"}
    )
    await repo.apply_tag_changes(
        "wpid_other", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "green"}
    )

    rows = await _tag_value_rows(repo)
    assert len(rows) == 1
    assert rows[0].color == "green"


@pytest.mark.asyncio
async def test_color_override_applies_on_idempotent_set(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "blue"}
    )
    # Re-SET the same (key, value) on the same workflow: an idempotent tag op that emits no
    # tag event must still apply the new color override to the registry row.
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "green"}
    )

    rows = await _tag_value_rows(repo)
    assert len(rows) == 1
    assert rows[0].color == "green"


@pytest.mark.asyncio
async def test_distinct_values_under_same_key_get_distinct_color_rows(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "blue"}
    )
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx(), colors={"env": "red"}
    )

    rows = await _tag_value_rows(repo)
    assert {(r.value, r.color) for r in rows} == {("prod", "blue"), ("stg", "red")}


@pytest.mark.asyncio
async def test_standalone_labels_are_not_colored(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(WPID, ORG_ID, sets={}, deletes=set(), context=_ctx(), label_sets=["urgent"])
    assert await _tag_value_rows(repo) == []


@pytest.mark.asyncio
async def test_list_tag_values_returns_active_rows_ordered(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        WPID,
        ORG_ID,
        sets={"team": "core", "env": "prod"},
        deletes=set(),
        context=_ctx(),
        colors={"team": "purple", "env": "blue"},
    )

    values = await repo.list_tag_values(ORG_ID)
    assert [(v.key, v.value, v.color) for v in values] == [("env", "prod", "blue"), ("team", "core", "purple")]


@pytest.mark.asyncio
async def test_list_tag_values_is_org_scoped(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "blue"}
    )
    assert await repo.list_tag_values("o_someone_else") == []


@pytest.mark.asyncio
async def test_recolor_tag_value_updates_color(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "blue"}
    )

    updated = await repo.recolor_tag_value(ORG_ID, "env", "prod", "pink")
    assert updated is not None
    assert updated.color == "pink"
    assert [v.color for v in await repo.list_tag_values(ORG_ID)] == ["pink"]


@pytest.mark.asyncio
async def test_recolor_tag_value_returns_none_when_absent(repo: TagsRepository) -> None:
    assert await repo.recolor_tag_value(ORG_ID, "env", "prod", "pink") is None


@pytest.mark.asyncio
async def test_delete_tag_key_soft_deletes_value_colors(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        WPID, ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "blue"}
    )
    assert len(await repo.list_tag_values(ORG_ID)) == 1

    await repo.delete_tag_key(ORG_ID, "env", _ctx())
    # The key's color rows must not survive the key delete and keep showing in GET /tag-values.
    assert await repo.list_tag_values(ORG_ID) == []


# ---------------------- SKY-11336: per-label count, rename, soft-delete ----------------------


@pytest.mark.asyncio
async def test_count_active_workflows_per_value(repo: TagsRepository) -> None:
    for wpid in ("wpid_a", "wpid_b", "wpid_c"):
        await _seed_workflow(repo, wpid)
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_b", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_c", ORG_ID, sets={"env": "stg", "team": "core"}, deletes=set(), context=_ctx())

    counts = await repo.count_active_workflows_per_value(ORG_ID)
    assert counts == {("env", "prod"): 2, ("env", "stg"): 1, ("team", "core"): 1}


@pytest.mark.asyncio
async def test_count_active_workflows_per_value_excludes_soft_deleted_workflows(repo: TagsRepository) -> None:
    await _seed_workflow(repo, "wpid_live")
    await _seed_workflow(repo, "wpid_gone", deleted=True)
    await repo.apply_tag_changes("wpid_live", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_gone", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())

    # The soft-deleted workflow's lingering SET event must not inflate the count.
    assert await repo.count_active_workflows_per_value(ORG_ID) == {("env", "prod"): 1}
    assert await repo.count_active_workflows_for_value(ORG_ID, "env", "prod") == 1


@pytest.mark.asyncio
async def test_count_active_workflows_per_value_ignores_deleted_and_standalone(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        "wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), label_sets=["urgent"]
    )
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={}, deletes={"env"}, context=_ctx())
    # The deleted grouped tag and the standalone label must not appear in per-value counts.
    assert await repo.count_active_workflows_per_value(ORG_ID) == {}


@pytest.mark.asyncio
async def test_count_active_workflows_for_value_targeted(repo: TagsRepository) -> None:
    for wpid in ("wpid_a", "wpid_b", "wpid_c"):
        await _seed_workflow(repo, wpid)
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_b", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_c", ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx())

    assert await repo.count_active_workflows_for_value(ORG_ID, "env", "prod") == 2
    assert await repo.count_active_workflows_for_value(ORG_ID, "env", "stg") == 1
    # Unknown pair and other org both count zero.
    assert await repo.count_active_workflows_for_value(ORG_ID, "env", "dev") == 0
    assert await repo.count_active_workflows_for_value("o_other", "env", "prod") == 0


@pytest.mark.asyncio
async def test_rename_tag_value_cascades_and_carries_color(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        "wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "blue"}
    )
    await repo.apply_tag_changes(
        "wpid_b", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "blue"}
    )

    result = await repo.rename_tag_value(ORG_ID, "env", "prod", "production", _ctx())

    assert result is not None
    assert (result.key, result.value, result.color) == ("env", "production", "blue")
    assert result.renamed_workflow_count == 2
    # Both workflows now carry the new value...
    assert await repo.get_active_grouped_tags_for_workflow("wpid_a", ORG_ID) == {"env": "production"}
    assert await repo.get_active_grouped_tags_for_workflow("wpid_b", ORG_ID) == {"env": "production"}
    # ...the new color row replaces the old one, carrying the color forward.
    assert [(v.key, v.value, v.color) for v in await repo.list_tag_values(ORG_ID)] == [("env", "production", "blue")]


@pytest.mark.asyncio
async def test_rename_tag_value_cascades_across_workflow_runs(repo: TagsRepository) -> None:
    await _seed_workflow_run(repo, "wr_a")
    await _seed_workflow_run(repo, "wr_b")
    await repo.apply_run_tag_changes("wr_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_run_tag_changes("wr_b", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())

    result = await repo.rename_tag_value(ORG_ID, "env", "prod", "production", _ctx())

    assert result is not None
    assert result.renamed_workflow_count == 0
    assert await repo.get_active_grouped_tags_for_run("wr_a", ORG_ID) == {"env": "production"}
    assert await repo.get_active_grouped_tags_for_run("wr_b", ORG_ID) == {"env": "production"}
    assert [(v.key, v.value) for v in await repo.list_tag_values(ORG_ID)] == [("env", "production")]


@pytest.mark.asyncio
async def test_rename_tag_value_preserves_history(repo: TagsRepository) -> None:
    """Append-only invariant: the old value's SET event stays in history (superseded),
    a fresh SET records the new value — past events are never rewritten."""
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.rename_tag_value(ORG_ID, "env", "prod", "production", _ctx())

    history = await repo.get_tag_event_history("wpid_a", ORG_ID)
    values = [(row.event_type, row.value) for row in history]
    # Newest-first: the new SET, then the superseded old SET. The old value is preserved.
    assert values == [(TagEventType.SET.value, "production"), (TagEventType.SET.value, "prod")]
    assert history[1].superseded_at is not None


@pytest.mark.asyncio
async def test_rename_tag_value_unknown_returns_none(repo: TagsRepository) -> None:
    assert await repo.rename_tag_value(ORG_ID, "env", "prod", "production", _ctx()) is None


@pytest.mark.asyncio
async def test_rename_tag_value_rejects_org_wide_collision(repo: TagsRepository) -> None:
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_b", ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx())

    with pytest.raises(TagValueRenameCollision):
        await repo.rename_tag_value(ORG_ID, "env", "prod", "stg", _ctx())
    # The cascade must not have run: prod is untouched.
    assert await repo.get_active_grouped_tags_for_workflow("wpid_a", ORG_ID) == {"env": "prod"}


@pytest.mark.asyncio
async def test_rename_tag_value_collision_detects_in_use_value(repo: TagsRepository) -> None:
    """Collision is org-wide: a target value in use on a workflow (active SET) blocks
    the rename even when its color row was soft-deleted out of band."""
    from sqlalchemy import update as _update

    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_b", ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx())
    # Drop only the stg color row; the active SET still makes stg exist org-wide.
    async with repo.Session() as session:
        await session.execute(
            _update(TagValueModel)
            .where(TagValueModel.key == "env", TagValueModel.value == "stg")
            .values(deleted_at=datetime.now(timezone.utc))
        )
        await session.commit()

    with pytest.raises(TagValueRenameCollision):
        await repo.rename_tag_value(ORG_ID, "env", "prod", "stg", _ctx())


@pytest.mark.asyncio
async def test_rename_tag_value_collision_detects_in_use_run_value(repo: TagsRepository) -> None:
    """A target value in use on a workflow run blocks rename even if its color row was soft-deleted."""
    from sqlalchemy import update as _update

    await _seed_workflow_run(repo, "wr_source")
    await _seed_workflow_run(repo, "wr_target")
    await repo.apply_run_tag_changes("wr_source", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_run_tag_changes("wr_target", ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx())
    async with repo.Session() as session:
        await session.execute(
            _update(TagValueModel)
            .where(TagValueModel.key == "env", TagValueModel.value == "stg")
            .values(deleted_at=datetime.now(timezone.utc))
        )
        await session.commit()

    with pytest.raises(TagValueRenameCollision):
        await repo.rename_tag_value(ORG_ID, "env", "prod", "stg", _ctx())


@pytest.mark.asyncio
async def test_rename_tag_value_reuses_soft_deleted_target(repo: TagsRepository) -> None:
    """A previously soft-deleted target value is NOT a collision — rename succeeds."""
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_b", ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx())
    # Remove stg entirely (color row soft-deleted, SET superseded).
    await repo.delete_tag_value(ORG_ID, "env", "stg", _ctx())

    result = await repo.rename_tag_value(ORG_ID, "env", "prod", "stg", _ctx())
    assert result is not None
    assert await repo.get_active_grouped_tags_for_workflow("wpid_a", ORG_ID) == {"env": "stg"}


@pytest.mark.asyncio
async def test_rename_tag_value_carries_random_color_when_no_color_row(repo: TagsRepository) -> None:
    """If the source has active SETs but no color row, the rename still registers the
    new label with a palette color rather than failing."""
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    async with repo.Session() as session:
        from sqlalchemy import update as _update

        await session.execute(
            _update(TagValueModel)
            .where(TagValueModel.key == "env", TagValueModel.value == "prod")
            .values(deleted_at=datetime.now(timezone.utc))
        )
        await session.commit()

    result = await repo.rename_tag_value(ORG_ID, "env", "prod", "production", _ctx())
    assert result is not None
    assert result.color in TAG_COLOR_PALETTE
    assert result.renamed_workflow_count == 1


@pytest.mark.asyncio
async def test_delete_tag_value_cascades_and_counts(repo: TagsRepository) -> None:
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_b", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_tag_changes("wpid_c", ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx())

    removed = await repo.delete_tag_value(ORG_ID, "env", "prod", _ctx())

    assert removed == 2
    assert await repo.get_active_grouped_tags_for_workflow("wpid_a", ORG_ID) == {}
    assert await repo.get_active_grouped_tags_for_workflow("wpid_b", ORG_ID) == {}
    # A sibling value under the same key is untouched.
    assert await repo.get_active_grouped_tags_for_workflow("wpid_c", ORG_ID) == {"env": "stg"}


@pytest.mark.asyncio
async def test_delete_tag_value_cascades_across_workflow_runs(repo: TagsRepository) -> None:
    for wrid in ("wr_a", "wr_b", "wr_c"):
        await _seed_workflow_run(repo, wrid)

    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_run_tag_changes("wr_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_run_tag_changes("wr_b", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.apply_run_tag_changes("wr_c", ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx())

    removed_workflows = await repo.delete_tag_value(ORG_ID, "env", "prod", _ctx())

    assert removed_workflows == 1
    assert await repo.get_active_grouped_tags_for_workflow("wpid_a", ORG_ID) == {}
    assert await repo.get_active_grouped_tags_for_run("wr_a", ORG_ID) == {}
    assert await repo.get_active_grouped_tags_for_run("wr_b", ORG_ID) == {}
    assert await repo.get_active_grouped_tags_for_run("wr_c", ORG_ID) == {"env": "stg"}
    rows = await _all_run_events(repo)
    run_deletes = [row for row in rows if row.event_type == TagEventType.DELETE.value]
    assert {(row.workflow_run_id, row.key, row.value) for row in run_deletes} == {
        ("wr_a", "env", "prod"),
        ("wr_b", "env", "prod"),
    }


@pytest.mark.asyncio
async def test_delete_tag_value_writes_delete_event_carrying_value(repo: TagsRepository) -> None:
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.delete_tag_value(ORG_ID, "env", "prod", _ctx())

    rows = await _all_events(repo)
    assert rows[-1].event_type == TagEventType.DELETE.value
    # Per-value delete records which value was removed (vs the whole-key delete's null value).
    assert rows[-1].key == "env"
    assert rows[-1].value == "prod"


@pytest.mark.asyncio
async def test_delete_tag_value_soft_deletes_only_that_color_row(repo: TagsRepository) -> None:
    await repo.apply_tag_changes(
        "wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "blue"}
    )
    await repo.apply_tag_changes(
        "wpid_b", ORG_ID, sets={"env": "stg"}, deletes=set(), context=_ctx(), colors={"env": "red"}
    )

    await repo.delete_tag_value(ORG_ID, "env", "prod", _ctx())
    # prod's color row is gone; stg's survives.
    assert [(v.value, v.color) for v in await repo.list_tag_values(ORG_ID)] == [("stg", "red")]


@pytest.mark.asyncio
async def test_delete_tag_value_unknown_returns_none(repo: TagsRepository) -> None:
    assert await repo.delete_tag_value(ORG_ID, "env", "prod", _ctx()) is None


@pytest.mark.asyncio
async def test_delete_tag_value_idempotent(repo: TagsRepository) -> None:
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())

    assert await repo.delete_tag_value(ORG_ID, "env", "prod", _ctx()) == 1
    # Second call: no active SET, no active color row left.
    assert await repo.delete_tag_value(ORG_ID, "env", "prod", _ctx()) is None


@pytest.mark.asyncio
async def test_delete_tag_value_then_reapply_reregisters(repo: TagsRepository) -> None:
    await repo.apply_tag_changes("wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx())
    await repo.delete_tag_value(ORG_ID, "env", "prod", _ctx())

    await repo.apply_tag_changes(
        "wpid_a", ORG_ID, sets={"env": "prod"}, deletes=set(), context=_ctx(), colors={"env": "green"}
    )
    assert await repo.get_active_grouped_tags_for_workflow("wpid_a", ORG_ID) == {"env": "prod"}
    assert [(v.value, v.color) for v in await repo.list_tag_values(ORG_ID)] == [("prod", "green")]

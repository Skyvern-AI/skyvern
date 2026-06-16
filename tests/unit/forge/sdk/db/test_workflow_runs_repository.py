"""Tests for WorkflowRunsRepository.create_workflow_run_parameters batch method."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from skyvern.forge.sdk.db.repositories.workflow_runs import WorkflowRunsRepository
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType


def _make_workflow_parameter(
    key: str,
    *,
    workflow_parameter_type: WorkflowParameterType = WorkflowParameterType.STRING,
    default_value: str | int | float | bool | dict | list | None = None,
) -> WorkflowParameter:
    now = datetime.now(tz=timezone.utc)
    return WorkflowParameter(
        workflow_parameter_id=f"wp_{key}",
        workflow_id="wf_test",
        key=key,
        workflow_parameter_type=workflow_parameter_type,
        default_value=default_value,
        created_at=now,
        modified_at=now,
    )


class _SessionContext:
    def __init__(self, session: MagicMock) -> None:
        self._session = session

    async def __aenter__(self) -> MagicMock:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _Result:
    def __init__(self, value: Any) -> None:
        self._value = value

    def first(self) -> Any:
        return self._value


class _EmptyExecuteResult:
    def mappings(self) -> _EmptyExecuteResult:
        return self

    def all(self) -> list[Any]:
        return []


def _where_clause_sql(query: Any) -> str:
    return str(query.whereclause.compile(compile_kwargs={"literal_binds": True}))


def _assert_not_filtering_copilot_authored_workflows(where_clause: str) -> None:
    assert "workflows.created_by" not in where_clause
    assert "workflows.edited_by" not in where_clause


@pytest.mark.asyncio
async def test_batch_create_uses_add_all_flush_commit_not_refresh() -> None:
    """Batch insert should use add_all + flush + commit and never call refresh."""
    tracked_models: list = []
    session = MagicMock()
    session.add_all = MagicMock(side_effect=lambda models: tracked_models.extend(models))

    async def _flush() -> None:
        now = datetime.now(tz=timezone.utc)
        for model in tracked_models:
            model.created_at = now

    session.flush = AsyncMock(side_effect=_flush)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    string_param = _make_workflow_parameter("url")
    int_param = _make_workflow_parameter("count", workflow_parameter_type=WorkflowParameterType.INTEGER)

    created = await repo.create_workflow_run_parameters(
        workflow_run_id="wr_test",
        workflow_parameter_values=[
            (string_param, "https://example.com"),
            (int_param, "7"),
        ],
    )

    session.add_all.assert_called_once()
    session.flush.assert_awaited_once()
    session.commit.assert_awaited_once()
    session.refresh.assert_not_awaited()

    assert [p.workflow_parameter_id for p in created] == [
        string_param.workflow_parameter_id,
        int_param.workflow_parameter_id,
    ]
    assert [p.value for p in created] == ["https://example.com", 7]
    assert all(p.created_at is not None for p in created)


@pytest.mark.asyncio
async def test_batch_create_with_empty_list_returns_empty() -> None:
    """create_workflow_run_parameters with an empty list should short-circuit and return []."""
    session = MagicMock()
    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    result = await repo.create_workflow_run_parameters(
        workflow_run_id="wr_test",
        workflow_parameter_values=[],
    )

    assert result == []
    session.add_all.assert_not_called()


@pytest.mark.asyncio
async def test_batch_create_propagates_sqlalchemy_error_from_flush() -> None:
    """When flush() raises an IntegrityError, it should propagate without being swallowed."""
    db_error = IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
    session = MagicMock()
    session.add_all = MagicMock()
    session.flush = AsyncMock(side_effect=db_error)
    session.commit = AsyncMock()

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    param = _make_workflow_parameter("url")

    with pytest.raises(IntegrityError) as exc_info:
        await repo.create_workflow_run_parameters(
            workflow_run_id="wr_test",
            workflow_parameter_values=[(param, "https://example.com")],
        )

    assert exc_info.value is db_error
    session.add_all.assert_called_once()
    session.flush.assert_awaited_once()
    # commit should NOT be called when flush fails
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_create_propagates_sqlalchemy_error_from_commit() -> None:
    """When commit() raises an IntegrityError, it should propagate without being swallowed."""
    db_error = IntegrityError("INSERT", {}, Exception("FK constraint failed"))
    tracked_models: list = []

    session = MagicMock()
    session.add_all = MagicMock(side_effect=lambda models: tracked_models.extend(models))

    async def _flush() -> None:
        now = datetime.now(tz=timezone.utc)
        for model in tracked_models:
            model.created_at = now

    session.flush = AsyncMock(side_effect=_flush)
    session.commit = AsyncMock(side_effect=db_error)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    param = _make_workflow_parameter("url")

    with pytest.raises(IntegrityError) as exc_info:
        await repo.create_workflow_run_parameters(
            workflow_run_id="wr_test",
            workflow_parameter_values=[(param, "https://example.com")],
        )

    assert exc_info.value is db_error
    session.flush.assert_awaited_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_all_runs_v2_search_key_matches_run_id_and_workflow_permanent_id() -> None:
    """Regression test for SKY-8795: searching by run_id (wr_*/tsk_*) or wpid_*
    on the global runs page must match the underlying ID columns, not only
    `searchable_text` (which contains only title + url)."""
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_all_runs_v2(organization_id="o_test", search_key="wr_abc123")

    # Inspect the WHERE clause specifically — both columns are also in the SELECT
    # list, so a substring check on the full SQL would be a false positive.
    where_clause = _where_clause_sql(captured["query"])
    assert "task_runs.run_id" in where_clause
    # WPID search must match across both task_runs and the joined workflow_runs
    # so legacy rows with task_runs.workflow_permanent_id=NULL still hit.
    assert "coalesce(task_runs.workflow_permanent_id, workflow_runs.workflow_permanent_id)" in where_clause
    # autoescape rewrites '_' to e.g. '/_' so check the distinctive suffix.
    assert "abc123" in where_clause


@pytest.mark.asyncio
async def test_get_workflow_runs_with_pending_webhook_delivery_filters_finalized_null_webhook_state() -> None:
    captured: dict[str, Any] = {}

    async def _scalars(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.scalars = AsyncMock(side_effect=_scalars)
    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_workflow_runs_with_pending_webhook_delivery(
        older_than=datetime(2026, 6, 1, 13, 20, tzinfo=timezone.utc),
        limit=17,
        in_progress_reason="Webhook delivery is in progress",
    )

    rendered = str(captured["query"].compile(compile_kwargs={"literal_binds": True}))
    where_clause = _where_clause_sql(captured["query"])
    assert "workflow_runs.status IN ('failed', 'terminated', 'canceled', 'timed_out', 'completed')" in where_clause
    for expected in [
        "workflow_runs.webhook_callback_url IS NOT NULL",
        "length(trim(workflow_runs.webhook_callback_url)) > 0",
        # SQLAlchemy omits parentheses in the rendered string; SQL operator
        # precedence keeps the modified_at cutoff bound to only the sentinel branch.
        "workflow_runs.webhook_failure_reason IS NULL OR workflow_runs.webhook_failure_reason = 'Webhook delivery is in progress' AND workflow_runs.modified_at <= '2026-06-01 13:20:00'",
        "workflow_runs.finished_at <= '2026-06-01 13:20:00' OR workflow_runs.finished_at IS NULL AND workflow_runs.modified_at <= '2026-06-01 13:20:00'",
    ]:
        assert expected in where_clause
    assert "ORDER BY coalesce(workflow_runs.finished_at, workflow_runs.modified_at) ASC" in rendered
    assert "LIMIT 17" in rendered


@pytest.mark.asyncio
async def test_claim_workflow_run_webhook_delivery_only_claims_null_state() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return SimpleNamespace(rowcount=1)

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.commit = AsyncMock()
    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    claimed = await repo.claim_workflow_run_webhook_delivery(
        workflow_run_id="wr_abc",
        in_progress_reason="Webhook delivery is in progress",
    )

    rendered = str(captured["query"].compile(compile_kwargs={"literal_binds": True}))
    assert claimed is True
    assert "UPDATE workflow_runs SET" in rendered
    assert "webhook_failure_reason='Webhook delivery is in progress'" in rendered
    assert "modified_at=" in rendered
    assert "workflow_runs.workflow_run_id = 'wr_abc'" in rendered
    assert "workflow_runs.webhook_failure_reason IS NULL" in rendered
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_workflow_run_webhook_delivery_can_reclaim_stale_in_progress_state() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return SimpleNamespace(rowcount=1)

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.commit = AsyncMock()
    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    claimed = await repo.claim_workflow_run_webhook_delivery(
        workflow_run_id="wr_abc",
        in_progress_reason="Webhook delivery is in progress",
        claim_older_than=datetime(2026, 6, 1, 13, 20, tzinfo=timezone.utc),
    )

    where_clause = _where_clause_sql(captured["query"])
    assert claimed is True
    assert "workflow_runs.workflow_run_id = 'wr_abc'" in where_clause
    assert (
        "workflow_runs.webhook_failure_reason IS NULL OR workflow_runs.webhook_failure_reason = "
        "'Webhook delivery is in progress' AND workflow_runs.modified_at <= '2026-06-01 13:20:00'"
    ) in where_clause
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_all_runs_v2_selects_workflow_deleted_flag() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_all_runs_v2(organization_id="o_test")

    rendered = str(captured["query"].compile(compile_kwargs={"literal_binds": True}))
    assert "AS workflow_deleted" in rendered
    # NOT EXISTS subquery against an active (non-deleted) workflows row.
    assert "NOT (EXISTS" in rendered
    assert "workflows.deleted_at IS NULL" in rendered
    # WPID must coalesce task_runs over workflow_runs so legacy rows where
    # task_runs.workflow_permanent_id is NULL still resolve via the join.
    assert "coalesce(task_runs.workflow_permanent_id, workflow_runs.workflow_permanent_id)" in rendered


@pytest.mark.asyncio
async def test_get_all_runs_v2_status_filter_uses_coalesced_effective_status() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_all_runs_v2(organization_id="o_test")

    where_clause = _where_clause_sql(captured["query"]).lower()
    assert "coalesce(workflow_runs.status, task_runs.status) is not null" in where_clause
    assert "task_runs.status is not null" not in where_clause


@pytest.mark.asyncio
async def test_get_all_runs_v2_excludes_copilot_session_workflow_runs() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_all_runs_v2(organization_id="o_test")

    where_clause = _where_clause_sql(captured["query"])
    assert "workflow_runs.copilot_session_id IS NULL" in where_clause
    _assert_not_filtering_copilot_authored_workflows(where_clause)


@pytest.mark.asyncio
async def test_get_all_runs_excludes_copilot_session_workflow_runs() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.scalars = AsyncMock(return_value=_EmptyExecuteResult())

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_all_runs(organization_id="o_test")

    where_clause = _where_clause_sql(captured["query"])
    assert "workflow_runs.copilot_session_id IS NULL" in where_clause
    _assert_not_filtering_copilot_authored_workflows(where_clause)


@pytest.mark.asyncio
async def test_workflow_run_history_queries_exclude_copilot_session_runs() -> None:
    captured_queries: list[Any] = []

    async def _execute(query):
        captured_queries.append(query)
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_workflow_runs(organization_id="o_test")
    await repo.get_workflow_runs_for_workflow_permanent_id(
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
        exclude_child_runs=True,
    )

    assert len(captured_queries) == 2
    for query in captured_queries:
        where_clause = _where_clause_sql(query)
        assert "workflow_runs.copilot_session_id IS NULL" in where_clause
        _assert_not_filtering_copilot_authored_workflows(where_clause)
    workflow_runs_for_workflow_clause = _where_clause_sql(captured_queries[1])
    assert "workflow_runs.parent_workflow_run_id IS NULL" in workflow_runs_for_workflow_clause


@pytest.mark.asyncio
async def test_get_workflow_runs_for_workflow_permanent_id_keeps_child_runs_by_default() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_workflow_runs_for_workflow_permanent_id(
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
    )

    where_clause = _where_clause_sql(captured["query"])
    assert "workflow_runs.parent_workflow_run_id IS NULL" not in where_clause
    assert "workflow_runs.copilot_session_id IS NULL" in where_clause


@pytest.mark.asyncio
async def test_get_workflow_runs_for_browser_session_filters_and_excludes() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_workflow_runs_for_browser_session(
        browser_session_id="pbs_abc123",
        organization_id="o_test",
        page=2,
        page_size=5,
    )

    where_clause = _where_clause_sql(captured["query"])
    assert "workflow_runs.browser_session_id = 'pbs_abc123'" in where_clause
    assert "workflow_runs.organization_id = 'o_test'" in where_clause
    assert "workflow_runs.parent_workflow_run_id IS NULL" in where_clause
    assert "workflow_runs.copilot_session_id IS NULL" in where_clause
    _assert_not_filtering_copilot_authored_workflows(where_clause)

    rendered = str(captured["query"].compile(compile_kwargs={"literal_binds": True}))
    assert "ORDER BY workflow_runs.created_at DESC" in rendered
    assert "LIMIT 5" in rendered
    assert "OFFSET 5" in rendered


@pytest.mark.asyncio
async def test_get_blocking_sequential_workflow_run_scans_earlier_active_same_key() -> None:
    """SKY-10799: the sequential gate scans ALL earlier-queued same-key runs still in flight
    (queued/running/paused) — not a single depends_on edge — so it holds under a forest-shaped
    graph or a canceled predecessor. Earlier = (queued_at, id) strictly before self; queued_at
    is stamped under the submit lock, so it is the true queue order even when creation order
    diverges from submission order."""
    fake_run = MagicMock()
    fake_run.workflow_run_id = "wr_self"
    fake_run.organization_id = "o_test"
    fake_run.workflow_permanent_id = "wpid_test"
    fake_run.sequential_key = "cred_test-sequential-key"
    fake_run.browser_session_id = None
    fake_run.browser_address = None
    fake_run.created_at = datetime(2026, 6, 8, 18, 53, 48, tzinfo=timezone.utc)
    fake_run.queued_at = datetime(2026, 6, 8, 18, 53, 50, tzinfo=timezone.utc)

    calls: list[Any] = []

    async def _scalars(query: Any) -> Any:
        calls.append(query)
        # 1st query loads the run itself; 2nd is the gate scan we assert on.
        return _Result(fake_run) if len(calls) == 1 else _Result(None)

    session = MagicMock()
    session.scalars = AsyncMock(side_effect=_scalars)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    blocker = await repo.get_blocking_sequential_workflow_run("wr_self")
    assert blocker is None

    gate_query = calls[1]
    where_clause = _where_clause_sql(gate_query)
    rendered = str(gate_query.compile(compile_kwargs={"literal_binds": True}))

    # scoped to the same wpid + key, ignoring browser-session runs
    assert "workflow_runs.workflow_permanent_id = 'wpid_test'" in where_clause
    assert "workflow_runs.sequential_key = 'cred_test-sequential-key'" in where_clause
    assert "workflow_runs.browser_session_id IS NULL" in where_clause
    assert "workflow_runs.organization_id = 'o_test'" in where_clause
    # only genuinely in-flight runs block; `created` and terminal statuses do not
    assert "'queued'" in where_clause and "'running'" in where_clause and "'paused'" in where_clause
    assert "'created'" not in where_clause and "'completed'" not in where_clause
    # earlier-queued by (queued_at, id), never created_at, and FIFO order so the
    # earliest blocker surfaces; unqueued rows can't block
    assert "workflow_runs.queued_at IS NOT NULL" in where_clause
    assert "workflow_runs.queued_at <" in where_clause
    assert "workflow_runs.created_at" not in where_clause
    assert "workflow_runs.workflow_run_id < 'wr_self'" in where_clause
    assert "ORDER BY workflow_runs.queued_at ASC" in rendered


@pytest.mark.asyncio
async def test_get_blocking_sequential_workflow_run_prefers_browser_session_lane() -> None:
    """The gate's lane resolution must mirror enqueue priority (browser_session_id >
    browser_address > sequential_key): a non-debug run carrying both a browser session and a
    sequential_key chains on the session lane at enqueue, so the gate must scan that
    same lane or it can miss its actual blocker."""
    fake_run = MagicMock()
    fake_run.workflow_run_id = "wr_self"
    fake_run.organization_id = "o_test"
    fake_run.workflow_permanent_id = "wpid_test"
    fake_run.sequential_key = "cred_test-sequential-key"
    fake_run.browser_session_id = "pbs_test"
    fake_run.debug_session_id = None
    fake_run.browser_address = None
    fake_run.created_at = datetime(2026, 6, 8, 18, 53, 48, tzinfo=timezone.utc)
    fake_run.queued_at = datetime(2026, 6, 8, 18, 53, 50, tzinfo=timezone.utc)

    calls: list[Any] = []

    async def _scalars(query: Any) -> Any:
        calls.append(query)
        return _Result(fake_run) if len(calls) == 1 else _Result(None)

    session = MagicMock()
    session.scalars = AsyncMock(side_effect=_scalars)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_blocking_sequential_workflow_run("wr_self")

    where_clause = _where_clause_sql(calls[1])
    assert "workflow_runs.browser_session_id = 'pbs_test'" in where_clause
    assert "sequential_key" not in where_clause


@pytest.mark.asyncio
async def test_get_blocking_sequential_workflow_run_debug_session_uses_key_lane() -> None:
    """A debug-session run carries a browser_session_id but enqueue keeps it out of the
    browser-session lane (is_browser_session_workflow requires not debug_session_id). The gate
    must do the same, or the debug run scans only its own session and misses an earlier same-key
    run — the SKY-10799 regression."""
    fake_run = MagicMock()
    fake_run.workflow_run_id = "wr_self"
    fake_run.organization_id = "o_test"
    fake_run.workflow_permanent_id = "wpid_test"
    fake_run.sequential_key = "cred_test-sequential-key"
    fake_run.browser_session_id = "pbs_test"
    fake_run.debug_session_id = "dbg_test"
    fake_run.browser_address = None
    fake_run.created_at = datetime(2026, 6, 8, 18, 53, 48, tzinfo=timezone.utc)
    fake_run.queued_at = datetime(2026, 6, 8, 18, 53, 50, tzinfo=timezone.utc)

    calls: list[Any] = []

    async def _scalars(query: Any) -> Any:
        calls.append(query)
        return _Result(fake_run) if len(calls) == 1 else _Result(None)

    session = MagicMock()
    session.scalars = AsyncMock(side_effect=_scalars)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_blocking_sequential_workflow_run("wr_self")

    where_clause = _where_clause_sql(calls[1])
    assert "workflow_runs.sequential_key = 'cred_test-sequential-key'" in where_clause
    assert "workflow_runs.browser_session_id IS NULL" in where_clause
    assert "workflow_runs.browser_session_id = 'pbs_test'" not in where_clause

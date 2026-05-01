"""Tests for WorkflowRunsRepository.create_workflow_run_parameters batch method."""

from __future__ import annotations

from datetime import datetime, timezone
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
    assert "task_runs.workflow_permanent_id" in where_clause
    # autoescape rewrites '_' to e.g. '/_' so check the distinctive suffix.
    assert "abc123" in where_clause


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
    )

    assert len(captured_queries) == 2
    for query in captured_queries:
        where_clause = _where_clause_sql(query)
        assert "workflow_runs.copilot_session_id IS NULL" in where_clause
        _assert_not_filtering_copilot_authored_workflows(where_clause)

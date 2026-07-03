from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.db.repositories.workflow_runs import WorkflowRunsRepository


class _SessionContext:
    def __init__(self, session: MagicMock) -> None:
        self._session = session

    async def __aenter__(self) -> MagicMock:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _Result:
    def all(self):
        return []


def _make_repo(captured: dict[str, Any]) -> WorkflowRunsRepository:
    async def _execute(query):
        captured["query"] = query
        return _Result()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)
    return WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)


@pytest.mark.asyncio
async def test_get_workflow_runs_filters_by_created_at_window() -> None:
    captured: dict[str, Any] = {}
    repo = _make_repo(captured)

    await repo.get_workflow_runs_for_workflow_permanent_id(
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
        created_at_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        created_at_end=datetime(2026, 6, 8, tzinfo=timezone.utc),
    )

    where = str(captured["query"].whereclause)
    assert "created_at >=" in where
    assert "created_at <" in where


@pytest.mark.asyncio
async def test_get_workflow_runs_omits_created_at_filter_when_unset() -> None:
    captured: dict[str, Any] = {}
    repo = _make_repo(captured)

    await repo.get_workflow_runs_for_workflow_permanent_id(
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
    )

    where = str(captured["query"].whereclause)
    assert "created_at >=" not in where
    assert "created_at <" not in where

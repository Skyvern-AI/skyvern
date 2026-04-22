"""Regression tests for SKY-8795: workflow list search must match workflow_permanent_id.

The /workflows page sends `search_key` to the backend. Before the fix, the search
only matched `workflows.title`, `folders.title`, and parameter metadata — typing a
`wpid_*` into the search box returned no results.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.db.repositories.workflows import WorkflowsRepository


class _SessionContext:
    def __init__(self, session: MagicMock) -> None:
        self._session = session

    async def __aenter__(self) -> MagicMock:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


@pytest.mark.asyncio
async def test_get_workflows_by_organization_id_search_key_matches_workflow_permanent_id() -> None:
    captured: dict[str, Any] = {}

    class _Scalars:
        def all(self):
            return []

    async def _scalars(query):
        captured["query"] = query
        return _Scalars()

    session = MagicMock()
    session.scalars = AsyncMock(side_effect=_scalars)

    repo = WorkflowsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_workflows_by_organization_id(
        organization_id="o_test",
        search_key="wpid_510867674757598984",
    )

    # Inspect the WHERE clause specifically — workflow_permanent_id is also in the
    # SELECT list, so a substring check on the full SQL would be a false positive.
    whereclause = captured["query"].whereclause
    assert whereclause is not None
    compiled_where = str(whereclause.compile(compile_kwargs={"literal_binds": True}))

    # The search filter must reference the workflow_permanent_id column directly so
    # that pasting a wpid_* into the workflows page search box finds the workflow.
    assert "workflows.workflow_permanent_id" in compiled_where
    assert "wpid_510867674757598984" in compiled_where

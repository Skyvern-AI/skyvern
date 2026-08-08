"""Regression tests: list-search ``search_key`` must escape SQL LIKE wildcards.

``get_workflows_by_organization_id``, ``get_all_runs`` and ``list_browser_profiles``
built their ``ilike`` filters from a raw ``f"%{search_key}%"``. Because ``_`` and
``%`` are LIKE wildcards, a search term like ``wr_abc`` (repo IDs are underscore
separated) matched far more rows than the literal text — e.g. ``wr_abc`` also
matched ``wrXabc``. The repo already had the correct pattern
(``icontains(search_key, autoescape=True)``, used by ``workflow_permanent_id`` and
``_apply_workflow_run_search_key_filter``); these three call sites just omitted it.

Each test compiles the WHERE clause and asserts the underscore is escaped
(``ab/_cd`` with an ``ESCAPE`` clause) rather than left as a bare wildcard.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.db.repositories.workflow_runs import WorkflowRunsRepository
from skyvern.forge.sdk.db.repositories.workflows import WorkflowsRepository

SEARCH_KEY = "ab_cd"


class _SessionContext:
    def __init__(self, session: MagicMock) -> None:
        self._session = session

    async def __aenter__(self) -> MagicMock:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _AllResult:
    def all(self) -> list[Any]:
        return []


def _assert_escaped(where_sql: str) -> None:
    # The escaped literal proves the searched column is wired up AND autoescaped.
    assert "ab/_cd" in where_sql, where_sql
    # No bare, unescaped occurrence of the search term may remain (the `_` would
    # otherwise be a single-char wildcard).
    assert "ab_cd" not in where_sql, where_sql
    assert "ESCAPE" in where_sql, where_sql


@pytest.mark.asyncio
async def test_get_workflows_by_organization_id_escapes_search_key() -> None:
    captured: dict[str, Any] = {}

    async def _scalars(query: Any) -> _AllResult:
        captured.setdefault("query", query)
        return _AllResult()

    session = MagicMock()
    session.scalars = AsyncMock(side_effect=_scalars)

    repo = WorkflowsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)
    await repo.get_workflows_by_organization_id(organization_id="o_test", search_key=SEARCH_KEY)

    where_sql = str(captured["query"].whereclause.compile(compile_kwargs={"literal_binds": True}))
    _assert_escaped(where_sql)


@pytest.mark.asyncio
async def test_get_all_runs_escapes_search_key() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query: Any) -> _AllResult:
        captured.setdefault("query", query)
        return _AllResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.scalars = AsyncMock(side_effect=lambda query: _AllResult())

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)
    await repo.get_all_runs(organization_id="o_test", search_key=SEARCH_KEY)

    where_sql = str(captured["query"].whereclause.compile(compile_kwargs={"literal_binds": True}))
    _assert_escaped(where_sql)


@pytest.mark.asyncio
async def test_list_browser_profiles_escapes_search_key() -> None:
    captured: dict[str, Any] = {}

    async def _scalars(query: Any) -> _AllResult:
        captured.setdefault("query", query)
        return _AllResult()

    session = MagicMock()
    session.scalars = AsyncMock(side_effect=_scalars)

    repo = BrowserSessionsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)
    await repo.list_browser_profiles(organization_id="o_test", search_key=SEARCH_KEY)

    where_sql = str(captured["query"].whereclause.compile(compile_kwargs={"literal_binds": True}))
    _assert_escaped(where_sql)

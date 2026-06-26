"""Regression: `update_workflow_run_if_not_final` must refresh after `save_workflow_run_logs` so the sync converter doesn't trigger an async lazy-load (MissingGreenlet)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.db.repositories.workflow_runs import WorkflowRunsRepository
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from tests.unit.conftest import MockAsyncSessionCtx


def _make_repo_with_session(mock_workflow_run: MagicMock) -> tuple[WorkflowRunsRepository, AsyncMock]:
    scalars_result = MagicMock()
    scalars_result.one.return_value = mock_workflow_run

    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = mock_workflow_run.workflow_run_id

    session = AsyncMock()
    session.execute = AsyncMock(return_value=execute_result)
    session.scalars = AsyncMock(return_value=scalars_result)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    repo = WorkflowRunsRepository(session_factory=lambda: MockAsyncSessionCtx(session))
    return repo, session


@pytest.mark.asyncio
async def test_refreshes_loaded_model_after_save_logs() -> None:
    mock_workflow_run = MagicMock()
    mock_workflow_run.workflow_run_id = "wr_test"

    repo, session = _make_repo_with_session(mock_workflow_run)

    with (
        patch(
            "skyvern.forge.sdk.db.repositories.workflow_runs.save_workflow_run_logs",
            new=AsyncMock(),
        ) as mock_save,
        patch(
            "skyvern.forge.sdk.db.repositories.workflow_runs.convert_to_workflow_run",
            return_value=MagicMock(),
        ),
    ):
        await repo.update_workflow_run_if_not_final(
            workflow_run_id="wr_test",
            status=WorkflowRunStatus.running,
        )

    mock_save.assert_awaited_once_with("wr_test")
    session.refresh.assert_awaited_once_with(mock_workflow_run)


@pytest.mark.asyncio
async def test_refresh_is_called_after_save_logs_not_before() -> None:
    """Order matters: refreshing before ``save_workflow_run_logs`` does nothing,
    because it's the nested commit inside save that does the expiring."""
    mock_workflow_run = MagicMock()
    mock_workflow_run.workflow_run_id = "wr_test"

    repo, session = _make_repo_with_session(mock_workflow_run)

    call_log: list[str] = []

    async def refresh_spy(_: object) -> None:
        call_log.append("refresh")

    async def save_spy(_: str) -> None:
        call_log.append("save")

    session.refresh.side_effect = refresh_spy

    with (
        patch(
            "skyvern.forge.sdk.db.repositories.workflow_runs.save_workflow_run_logs",
            new=save_spy,
        ),
        patch(
            "skyvern.forge.sdk.db.repositories.workflow_runs.convert_to_workflow_run",
            return_value=MagicMock(),
        ),
    ):
        await repo.update_workflow_run_if_not_final(
            workflow_run_id="wr_test",
            status=WorkflowRunStatus.running,
        )

    assert call_log == ["save", "refresh"], (
        f"expected save_workflow_run_logs to run before session.refresh, got {call_log!r}"
    )


@pytest.mark.asyncio
async def test_returns_none_without_calling_save_or_refresh_when_no_row_updated() -> None:
    """If no non-terminal row matched the conditional UPDATE we return None
    early; refresh and save_workflow_run_logs should not run."""
    scalars_result = MagicMock()
    scalars_result.one.return_value = MagicMock()

    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.execute = AsyncMock(return_value=execute_result)
    session.scalars = AsyncMock(return_value=scalars_result)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    repo = WorkflowRunsRepository(session_factory=lambda: MockAsyncSessionCtx(session))

    with (
        patch(
            "skyvern.forge.sdk.db.repositories.workflow_runs.save_workflow_run_logs",
            new=AsyncMock(),
        ) as mock_save,
        patch(
            "skyvern.forge.sdk.db.repositories.workflow_runs.convert_to_workflow_run",
            return_value=MagicMock(),
        ) as mock_convert,
    ):
        result = await repo.update_workflow_run_if_not_final(
            workflow_run_id="wr_test",
            status=WorkflowRunStatus.canceled,
        )

    assert result is None
    mock_save.assert_not_awaited()
    session.refresh.assert_not_awaited()
    mock_convert.assert_not_called()

"""Tests that the workflow save and run-completion paths fire the onboarding milestone hooks."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.agent_functions import AgentFunction


@pytest.fixture()
def base_agent_fn() -> AgentFunction:
    return AgentFunction()


class TestBaseAgentFunctionNoOps:
    """OSS base stubs are no-ops and never raise."""

    @pytest.mark.asyncio
    async def test_on_workflow_saved_noop(self, base_agent_fn: AgentFunction) -> None:
        result = await base_agent_fn.on_workflow_saved(
            organization_id="o_test",
            edited_by="u_test",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_on_workflow_run_completed_noop(self, base_agent_fn: AgentFunction) -> None:
        result = await base_agent_fn.on_workflow_run_completed(
            organization_id="o_test",
            workflow_id="wf_test",
        )
        assert result is None


class TestWorkflowSaveHookFires:
    """update_workflow_definition fires on_workflow_saved as a background task."""

    @pytest.mark.asyncio
    async def test_save_fires_on_workflow_saved(self) -> None:
        mock_agent_fn = MagicMock(spec=AgentFunction)
        mock_agent_fn.on_workflow_saved = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.organization_id = "o_123"

        mock_db = MagicMock()
        mock_db.workflows.update_workflow = AsyncMock(return_value=mock_workflow)

        with (
            patch("skyvern.forge.sdk.workflow.service.app") as mock_app,
        ):
            mock_app.AGENT_FUNCTION = mock_agent_fn
            mock_app.DATABASE = mock_db

            from skyvern.forge.sdk.workflow.service import WorkflowService

            svc = WorkflowService.__new__(WorkflowService)
            svc._background_tasks = set()
            await svc.update_workflow_definition(
                workflow_id="wf_1",
                organization_id="o_123",
                title="Test",
                edited_by="u_456",
            )
            await asyncio.sleep(0)

        mock_agent_fn.on_workflow_saved.assert_awaited_once_with(
            organization_id="o_123",
            edited_by="u_456",
        )

    @pytest.mark.asyncio
    async def test_save_passes_none_edited_by_when_unset(self) -> None:
        mock_agent_fn = MagicMock(spec=AgentFunction)
        mock_agent_fn.on_workflow_saved = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.organization_id = "o_123"

        mock_db = MagicMock()
        mock_db.workflows.update_workflow = AsyncMock(return_value=mock_workflow)

        with (
            patch("skyvern.forge.sdk.workflow.service.app") as mock_app,
        ):
            mock_app.AGENT_FUNCTION = mock_agent_fn
            mock_app.DATABASE = mock_db

            from skyvern.forge.sdk.workflow.service import WorkflowService

            svc = WorkflowService.__new__(WorkflowService)
            svc._background_tasks = set()
            await svc.update_workflow_definition(
                workflow_id="wf_1",
                organization_id="o_123",
            )
            await asyncio.sleep(0)

        mock_agent_fn.on_workflow_saved.assert_awaited_once_with(
            organization_id="o_123",
            edited_by=None,
        )


class TestWorkflowRunCompleteHookFires:
    """_update_workflow_run_status fires on_workflow_run_completed for final statuses."""

    @pytest.mark.asyncio
    async def test_run_complete_fires_hook(self) -> None:
        mock_agent_fn = MagicMock(spec=AgentFunction)
        mock_agent_fn.on_workflow_run_completed = AsyncMock()

        mock_workflow_run = MagicMock()
        mock_workflow_run.organization_id = "o_789"
        mock_workflow_run.workflow_id = "wf_1"
        mock_workflow_run.workflow_permanent_id = "wpid_1"
        mock_workflow_run.status = "completed"
        mock_workflow_run.started_at = datetime(2025, 1, 1, 0, 0, 5)
        mock_workflow_run.created_at = datetime(2025, 1, 1)
        mock_workflow_run.run_with = None
        mock_workflow_run.ai_fallback = None
        mock_workflow_run.trigger_type = None
        mock_workflow_run.workflow_schedule_id = None

        mock_status = MagicMock()
        mock_status.is_final.return_value = True

        mock_db = MagicMock()
        mock_db.workflow_runs.update_workflow_run = AsyncMock(return_value=mock_workflow_run)

        with (
            patch("skyvern.forge.sdk.workflow.service.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.service.extraction_cache") as mock_cache,
        ):
            mock_app.AGENT_FUNCTION = mock_agent_fn
            mock_app.DATABASE = mock_db
            mock_cache.clear_workflow_run = MagicMock()

            from skyvern.forge.sdk.workflow.service import WorkflowService

            svc = WorkflowService.__new__(WorkflowService)
            svc._background_tasks = set()
            svc._sync_task_run_from_workflow_run = AsyncMock()

            await svc._update_workflow_run_status(
                workflow_run_id="wr_1",
                status=mock_status,
            )
            await asyncio.sleep(0)

        mock_agent_fn.on_workflow_run_completed.assert_awaited_once_with(
            organization_id="o_789",
            workflow_id="wf_1",
            status=mock_status,
        )

    @pytest.mark.asyncio
    async def test_run_non_final_does_not_fire_hook(self) -> None:
        mock_agent_fn = MagicMock(spec=AgentFunction)
        mock_agent_fn.on_workflow_run_completed = AsyncMock()

        mock_workflow_run = MagicMock()
        mock_workflow_run.organization_id = "o_789"

        mock_status = MagicMock()
        mock_status.is_final.return_value = False

        mock_db = MagicMock()
        mock_db.workflow_runs.update_workflow_run = AsyncMock(return_value=mock_workflow_run)

        with (
            patch("skyvern.forge.sdk.workflow.service.app") as mock_app,
        ):
            mock_app.AGENT_FUNCTION = mock_agent_fn
            mock_app.DATABASE = mock_db

            from skyvern.forge.sdk.workflow.service import WorkflowService

            svc = WorkflowService.__new__(WorkflowService)
            svc._background_tasks = set()
            svc._sync_task_run_from_workflow_run = AsyncMock()

            await svc._update_workflow_run_status(
                workflow_run_id="wr_1",
                status=mock_status,
            )
            await asyncio.sleep(0)

        mock_agent_fn.on_workflow_run_completed.assert_not_awaited()

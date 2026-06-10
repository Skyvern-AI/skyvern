"""Tests that the workflow save and run-completion paths fire the onboarding milestone hooks."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import params as fastapi_params

from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest, WorkflowDefinitionYAML, WorkflowRequest


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

    @pytest.mark.asyncio
    async def test_validate_user_organization_membership_unknown(self, base_agent_fn: AgentFunction) -> None:
        result = await base_agent_fn.validate_user_organization_membership(
            user_id="u_test",
            organization_id="o_test",
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


def _yaml_request(title: str = "Funnel Workflow") -> WorkflowCreateYAMLRequest:
    return WorkflowCreateYAMLRequest(
        title=title,
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
    )


def _stubbed_workflow_service() -> tuple[object, MagicMock]:
    from skyvern.forge.sdk.workflow.service import WorkflowService

    saved_workflow = MagicMock()
    saved_workflow.workflow_id = "wf_new"

    svc = WorkflowService.__new__(WorkflowService)
    svc.create_workflow = AsyncMock(return_value=saved_workflow)
    svc.make_workflow_definition = AsyncMock(return_value=MagicMock())
    svc.validate_workflow_block_graph = MagicMock()
    svc._validate_payload_templates = MagicMock()
    svc.update_workflow_definition = AsyncMock(return_value=saved_workflow)
    svc.maybe_delete_cached_code = AsyncMock()
    return svc, saved_workflow


class TestCreateWorkflowFromRequestThreadsAttribution:
    """create_workflow_from_request must forward the actor so on_workflow_saved sees it."""

    @pytest.mark.asyncio
    async def test_create_flow_threads_attribution(self) -> None:
        svc, _ = _stubbed_workflow_service()
        organization = MagicMock()
        organization.organization_id = "o_123"

        await svc.create_workflow_from_request(
            organization=organization,
            request=_yaml_request(),
            created_by="u_456",
            edited_by="u_456",
        )

        create_kwargs = svc.create_workflow.await_args.kwargs
        assert create_kwargs.get("created_by") == "u_456"
        assert create_kwargs.get("edited_by") == "u_456"
        # on_workflow_saved fires inside update_workflow_definition and needs the actor.
        update_kwargs = svc.update_workflow_definition.await_args.kwargs
        assert update_kwargs.get("edited_by") == "u_456"

    @pytest.mark.asyncio
    async def test_update_flow_threads_attribution(self) -> None:
        svc, _ = _stubbed_workflow_service()
        existing = MagicMock()
        existing.version = 1
        existing.cdp_connect_headers = None
        existing.max_elapsed_time_minutes = None
        existing.folder_id = None
        existing.code_version = None
        existing.workflow_permanent_id = "wpid_1"
        svc.get_workflow_by_permanent_id = AsyncMock(return_value=existing)
        organization = MagicMock()
        organization.organization_id = "o_123"

        await svc.create_workflow_from_request(
            organization=organization,
            request=_yaml_request(),
            workflow_permanent_id="wpid_1",
            created_by="u_456",
            edited_by="u_456",
        )

        create_kwargs = svc.create_workflow.await_args.kwargs
        assert create_kwargs.get("created_by") == "u_456"
        assert create_kwargs.get("edited_by") == "u_456"
        update_kwargs = svc.update_workflow_definition.await_args.kwargs
        assert update_kwargs.get("edited_by") == "u_456"


class TestWorkflowRoutesThreadUser:
    """The UI create/update routes must resolve the caller and stamp created_by/edited_by."""

    @pytest.mark.asyncio
    async def test_create_workflow_route_passes_user(self) -> None:
        from skyvern.forge.sdk.routes.agent_protocol import create_workflow

        organization = MagicMock()
        organization.organization_id = "o_123"
        data = WorkflowRequest(json_definition=_yaml_request())

        with patch("skyvern.forge.sdk.routes.agent_protocol.app") as mock_app:
            mock_app.WORKFLOW_SERVICE.create_workflow_from_request = AsyncMock(return_value=MagicMock())
            await create_workflow(
                data=data,
                folder_id=None,
                current_org=organization,
                user_id="u_456",
            )
            kwargs = mock_app.WORKFLOW_SERVICE.create_workflow_from_request.await_args.kwargs

        assert kwargs.get("created_by") == "u_456"
        assert kwargs.get("edited_by") == "u_456"

    @pytest.mark.asyncio
    async def test_update_workflow_route_passes_user(self) -> None:
        from skyvern.forge.sdk.routes.agent_protocol import update_workflow

        organization = MagicMock()
        organization.organization_id = "o_123"
        data = WorkflowRequest(json_definition=_yaml_request())

        with patch("skyvern.forge.sdk.routes.agent_protocol.app") as mock_app:
            mock_app.WORKFLOW_SERVICE.create_workflow_from_request = AsyncMock(return_value=MagicMock())
            await update_workflow(
                data=data,
                workflow_id="wpid_1",
                current_org=organization,
                user_id="u_456",
            )
            kwargs = mock_app.WORKFLOW_SERVICE.create_workflow_from_request.await_args.kwargs

        assert kwargs.get("created_by") == "u_456"
        assert kwargs.get("edited_by") == "u_456"

    @pytest.mark.asyncio
    async def test_create_workflow_legacy_route_passes_user(self) -> None:
        from skyvern.forge.sdk.routes.agent_protocol import create_workflow_legacy

        organization = MagicMock()
        organization.organization_id = "o_123"
        raw_request = MagicMock()
        raw_request.body = AsyncMock(
            return_value=b"title: Funnel Workflow\nworkflow_definition:\n  parameters: []\n  blocks: []\n"
        )

        with patch("skyvern.forge.sdk.routes.agent_protocol.app") as mock_app:
            mock_app.WORKFLOW_SERVICE.create_workflow_from_request = AsyncMock(return_value=MagicMock())
            await create_workflow_legacy(
                request=raw_request,
                folder_id=None,
                current_org=organization,
                user_id="u_456",
            )
            kwargs = mock_app.WORKFLOW_SERVICE.create_workflow_from_request.await_args.kwargs

        assert kwargs.get("created_by") == "u_456"
        assert kwargs.get("edited_by") == "u_456"

    @pytest.mark.asyncio
    async def test_update_workflow_legacy_route_passes_user(self) -> None:
        from skyvern.forge.sdk.routes.agent_protocol import update_workflow_legacy

        organization = MagicMock()
        organization.organization_id = "o_123"
        raw_request = MagicMock()
        raw_request.body = AsyncMock(
            return_value=b"title: Funnel Workflow\nworkflow_definition:\n  parameters: []\n  blocks: []\n"
        )

        with patch("skyvern.forge.sdk.routes.agent_protocol.app") as mock_app:
            mock_app.WORKFLOW_SERVICE.create_workflow_from_request = AsyncMock(return_value=MagicMock())
            await update_workflow_legacy(
                request=raw_request,
                workflow_id="wpid_1",
                current_org=organization,
                user_id="u_456",
            )
            kwargs = mock_app.WORKFLOW_SERVICE.create_workflow_from_request.await_args.kwargs

        assert kwargs.get("created_by") == "u_456"
        assert kwargs.get("edited_by") == "u_456"

    def test_routes_wire_fail_open_user_dependency(self) -> None:
        from skyvern.forge.sdk.routes import agent_protocol

        for route_fn in (
            agent_protocol.create_workflow,
            agent_protocol.create_workflow_legacy,
            agent_protocol.update_workflow,
            agent_protocol.update_workflow_legacy,
        ):
            user_param = inspect.signature(route_fn).parameters.get("user_id")
            assert user_param is not None, f"{route_fn.__name__} is missing the user_id dependency"
            assert isinstance(user_param.default, fastapi_params.Depends)
            assert user_param.default.dependency is org_auth_service.get_current_user_id_or_none


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

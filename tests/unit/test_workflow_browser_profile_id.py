from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition, WorkflowRequestBody
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest, WorkflowDefinitionYAML


def _make_workflow(browser_profile_id: str | None = None) -> Workflow:
    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id="w_test",
        organization_id="o_test",
        title="test",
        workflow_permanent_id="wpid_test",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
        browser_profile_id=browser_profile_id,
        created_at=now,
        modified_at=now,
    )


def test_workflow_pydantic_defaults_to_none() -> None:
    workflow = _make_workflow()
    assert workflow.browser_profile_id is None


def test_workflow_pydantic_accepts_browser_profile_id() -> None:
    workflow = _make_workflow(browser_profile_id="bp_abc123")
    assert workflow.browser_profile_id == "bp_abc123"


def test_workflow_create_yaml_request_defaults_to_none() -> None:
    request = WorkflowCreateYAMLRequest(
        title="test",
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
    )
    assert request.browser_profile_id is None


def test_workflow_create_yaml_request_accepts_browser_profile_id() -> None:
    request = WorkflowCreateYAMLRequest(
        title="test",
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
        browser_profile_id="bp_abc123",
    )
    assert request.browser_profile_id == "bp_abc123"


def _make_setup_service(workflow: SimpleNamespace) -> tuple[WorkflowService, SimpleNamespace, SimpleNamespace]:
    service = WorkflowService()
    workflow_run = SimpleNamespace(workflow_run_id="wr_test", workflow_permanent_id="wpid_test")

    service.get_workflow_by_permanent_id = AsyncMock(return_value=workflow)  # type: ignore[method-assign]
    service.create_workflow_run = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]
    service.get_workflow_parameters = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service.create_workflow_run_parameters = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service.mark_workflow_run_as_failed = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]

    organization = SimpleNamespace(organization_id="org_test", organization_name="Test Org")
    return service, organization, workflow_run


def _make_workflow_stub(browser_profile_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        organization_id="org_test",
        proxy_location=None,
        webhook_callback_url=None,
        extra_http_headers=None,
        browser_profile_id=browser_profile_id,
        run_with="agent",
        code_version=None,
        adaptive_caching=False,
        sequential_key=None,
    )


@pytest.fixture(autouse=True)
def reset_context() -> None:
    skyvern_context.reset()
    yield
    skyvern_context.reset()


@pytest.mark.asyncio
async def test_setup_workflow_run_falls_back_to_workflow_browser_profile_id() -> None:
    """When the run-level browser_profile_id is unset, the workflow default is used."""
    workflow_stub = _make_workflow_stub(browser_profile_id="bp_default")
    service, organization, _ = _make_setup_service(workflow_stub)

    request = WorkflowRequestBody(data={})
    assert request.browser_profile_id is None

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)

        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=request,
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

    assert request.browser_profile_id == "bp_default"


@pytest.mark.asyncio
async def test_setup_workflow_run_run_level_value_takes_precedence() -> None:
    """An explicit run-level browser_profile_id overrides the workflow default."""
    workflow_stub = _make_workflow_stub(browser_profile_id="bp_default")
    service, organization, _ = _make_setup_service(workflow_stub)

    request = WorkflowRequestBody(data={}, browser_profile_id="bp_run_specific")

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)

        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=request,
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

    assert request.browser_profile_id == "bp_run_specific"


@pytest.mark.asyncio
async def test_setup_workflow_run_no_default_no_request_stays_none() -> None:
    """No workflow default and no run-level value preserves the existing None behavior."""
    workflow_stub = _make_workflow_stub(browser_profile_id=None)
    service, organization, _ = _make_setup_service(workflow_stub)

    request = WorkflowRequestBody(data={})

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)

        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=request,
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

    assert request.browser_profile_id is None


@pytest.mark.asyncio
async def test_setup_workflow_run_session_present_skips_workflow_default() -> None:
    """When a browser_session_id is supplied, the workflow default must not shadow session-derived precedence."""
    workflow_stub = _make_workflow_stub(browser_profile_id="bp_workflow_default")
    service, organization, _ = _make_setup_service(workflow_stub)

    request = WorkflowRequestBody(data={}, browser_session_id="pbs_xxx")
    assert request.browser_profile_id is None

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
        mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)

        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=request,
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

    assert request.browser_profile_id is None

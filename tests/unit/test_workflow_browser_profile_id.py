from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest

from skyvern.exceptions import SkyvernHTTPException
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


@pytest.mark.asyncio
async def test_create_workflow_from_request_rejects_raw_load_balancer_webhook_url() -> None:
    request = WorkflowCreateYAMLRequest(
        title="test",
        webhook_callback_url="https://service-123.elb.us-east-1.amazonaws.com/hook",
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
    )

    with pytest.raises(SkyvernHTTPException, match="stable custom hostname"):
        await WorkflowService().create_workflow_from_request(
            organization=cast(Any, SimpleNamespace(organization_id="org_1")),
            request=request,
        )


@pytest.mark.asyncio
async def test_create_workflow_from_request_allows_unchanged_legacy_webhook_url() -> None:
    legacy_url = "https://service-123.elb.us-east-1.amazonaws.com/hook"
    service, updated_workflow = _make_workflow_update_service(
        existing_max_elapsed_time_minutes=None,
        existing_webhook_callback_url=legacy_url,
    )
    request = WorkflowCreateYAMLRequest(
        title="test",
        webhook_callback_url=legacy_url,
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
    )

    result = await service.create_workflow_from_request(
        organization=cast(Any, SimpleNamespace(organization_id="org_1")),
        request=request,
        workflow_permanent_id="wpid_test",
    )

    assert result is updated_workflow


def test_workflow_create_yaml_request_masks_cdp_connect_headers_on_dump() -> None:
    request = WorkflowCreateYAMLRequest(
        title="test",
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
        cdp_connect_headers={"x-api-key": "secret", "authorization": "Bearer secret"},
    )

    assert request.cdp_connect_headers == {"x-api-key": "secret", "authorization": "Bearer secret"}
    assert request.model_dump()["cdp_connect_headers"] == {
        "x-api-key": "***",
        "authorization": "***",
    }


@pytest.mark.asyncio
async def test_create_workflow_from_request_preserves_existing_max_elapsed_time_when_omitted() -> None:
    service, updated_workflow = _make_workflow_update_service(existing_max_elapsed_time_minutes=90)

    request = WorkflowCreateYAMLRequest(
        title="test",
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
    )

    result = await service.create_workflow_from_request(
        organization=cast(Any, SimpleNamespace(organization_id="org_1")),
        request=request,
        workflow_permanent_id="wpid_test",
    )

    assert result is updated_workflow
    create_workflow_mock = service.create_workflow
    assert isinstance(create_workflow_mock, AsyncMock)
    create_workflow_mock.assert_awaited_once()
    assert create_workflow_mock.await_args is not None
    assert create_workflow_mock.await_args.kwargs["max_elapsed_time_minutes"] == 90
    refresh_schedules_mock = service._refresh_workflow_schedule_runtime_limits
    assert isinstance(refresh_schedules_mock, AsyncMock)
    refresh_schedules_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_workflow_from_request_preserves_existing_created_by_when_omitted() -> None:
    service, _ = _make_workflow_update_service(
        existing_max_elapsed_time_minutes=None,
        existing_created_by="o_1_user",
    )

    request = WorkflowCreateYAMLRequest(
        title="test",
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
    )

    await service.create_workflow_from_request(
        organization=cast(Any, SimpleNamespace(organization_id="org_1")),
        request=request,
        workflow_permanent_id="wpid_test",
        edited_by="copilot",
    )

    create_workflow_mock = service.create_workflow
    assert isinstance(create_workflow_mock, AsyncMock)
    assert create_workflow_mock.await_args is not None
    assert create_workflow_mock.await_args.kwargs["created_by"] == "o_1_user"
    assert create_workflow_mock.await_args.kwargs["edited_by"] == "copilot"


@pytest.mark.asyncio
async def test_create_workflow_from_request_preserves_enable_self_healing_when_omitted() -> None:
    service, _ = _make_workflow_update_service(
        existing_max_elapsed_time_minutes=None, existing_enable_self_healing=True
    )

    request = WorkflowCreateYAMLRequest(
        title="test",
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
    )

    await service.create_workflow_from_request(
        organization=cast(Any, SimpleNamespace(organization_id="org_1")),
        request=request,
        workflow_permanent_id="wpid_test",
    )

    create_workflow_mock = service.create_workflow
    assert isinstance(create_workflow_mock, AsyncMock)
    assert create_workflow_mock.await_args is not None
    assert create_workflow_mock.await_args.kwargs["enable_self_healing"] is True


@pytest.mark.asyncio
async def test_create_workflow_from_request_preserves_pin_saved_session_ip_when_omitted() -> None:
    service, _ = _make_workflow_update_service(
        existing_max_elapsed_time_minutes=None, existing_pin_saved_session_ip=True
    )

    request = WorkflowCreateYAMLRequest(
        title="test",
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
    )

    await service.create_workflow_from_request(
        organization=cast(Any, SimpleNamespace(organization_id="org_1")),
        request=request,
        workflow_permanent_id="wpid_test",
    )

    create_workflow_mock = service.create_workflow
    assert isinstance(create_workflow_mock, AsyncMock)
    assert create_workflow_mock.await_args is not None
    assert create_workflow_mock.await_args.kwargs["pin_saved_session_ip"] is True


@pytest.mark.asyncio
async def test_create_workflow_from_request_explicit_false_clears_pin_saved_session_ip() -> None:
    service, _ = _make_workflow_update_service(
        existing_max_elapsed_time_minutes=None, existing_pin_saved_session_ip=True
    )

    request = WorkflowCreateYAMLRequest(
        title="test",
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
        pin_saved_session_ip=False,
    )

    await service.create_workflow_from_request(
        organization=cast(Any, SimpleNamespace(organization_id="org_1")),
        request=request,
        workflow_permanent_id="wpid_test",
    )

    create_workflow_mock = service.create_workflow
    assert isinstance(create_workflow_mock, AsyncMock)
    assert create_workflow_mock.await_args is not None
    assert create_workflow_mock.await_args.kwargs["pin_saved_session_ip"] is False


@pytest.mark.asyncio
async def test_create_workflow_from_request_explicit_false_clears_enable_self_healing() -> None:
    service, _ = _make_workflow_update_service(
        existing_max_elapsed_time_minutes=None, existing_enable_self_healing=True
    )

    request = WorkflowCreateYAMLRequest(
        title="test",
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
        enable_self_healing=False,
    )

    await service.create_workflow_from_request(
        organization=cast(Any, SimpleNamespace(organization_id="org_1")),
        request=request,
        workflow_permanent_id="wpid_test",
    )

    create_workflow_mock = service.create_workflow
    assert isinstance(create_workflow_mock, AsyncMock)
    assert create_workflow_mock.await_args is not None
    assert create_workflow_mock.await_args.kwargs["enable_self_healing"] is False


@pytest.mark.asyncio
async def test_create_workflow_from_request_allows_explicit_null_to_clear_existing_max_elapsed_time() -> None:
    service, updated_workflow = _make_workflow_update_service(existing_max_elapsed_time_minutes=90)

    request = WorkflowCreateYAMLRequest(
        title="test",
        workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
        max_elapsed_time_minutes=None,
    )

    result = await service.create_workflow_from_request(
        organization=cast(Any, SimpleNamespace(organization_id="org_1")),
        request=request,
        workflow_permanent_id="wpid_test",
    )

    assert result is updated_workflow
    create_workflow_mock = service.create_workflow
    assert isinstance(create_workflow_mock, AsyncMock)
    create_workflow_mock.assert_awaited_once()
    assert create_workflow_mock.await_args is not None
    assert create_workflow_mock.await_args.kwargs["max_elapsed_time_minutes"] is None
    refresh_schedules_mock = service._refresh_workflow_schedule_runtime_limits
    assert isinstance(refresh_schedules_mock, AsyncMock)
    refresh_schedules_mock.assert_awaited_once_with(
        workflow_permanent_id="wpid_test",
        organization_id="org_1",
        max_elapsed_time_minutes=None,
    )


@pytest.mark.asyncio
async def test_refresh_workflow_schedule_runtime_limits_reupserts_backend_schedules() -> None:
    service = WorkflowService()
    schedule_with_backend = SimpleNamespace(
        backend_schedule_id="temporal_1",
        workflow_schedule_id="wfs_1",
        cron_expression="0 */6 * * *",
        timezone="UTC",
        enabled=True,
        parameters={"url": "https://example.com"},
    )
    schedule_without_backend = SimpleNamespace(
        backend_schedule_id=None,
        workflow_schedule_id="wfs_local",
        cron_expression="0 */12 * * *",
        timezone="UTC",
        enabled=False,
        parameters=None,
    )

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=None)
        mock_app.DATABASE.schedules.get_workflow_schedules = AsyncMock(
            return_value=[schedule_with_backend, schedule_without_backend]
        )
        mock_app.AGENT_FUNCTION.upsert_workflow_schedule = AsyncMock()

        await service._refresh_workflow_schedule_runtime_limits(
            workflow_permanent_id="wpid_test",
            organization_id="org_1",
            max_elapsed_time_minutes=360,
        )

    mock_app.DATABASE.schedules.get_workflow_schedules.assert_awaited_once_with(
        workflow_permanent_id="wpid_test",
        organization_id="org_1",
    )
    mock_app.AGENT_FUNCTION.upsert_workflow_schedule.assert_awaited_once_with(
        backend_schedule_id="temporal_1",
        organization_id="org_1",
        workflow_permanent_id="wpid_test",
        workflow_schedule_id="wfs_1",
        cron_expression="0 */6 * * *",
        timezone="UTC",
        enabled=True,
        parameters={"url": "https://example.com"},
        max_elapsed_time_minutes=360,
    )


def _make_workflow_update_service(
    existing_max_elapsed_time_minutes: int | None,
    existing_enable_self_healing: bool = True,
    existing_pin_saved_session_ip: bool = False,
    existing_webhook_callback_url: str | None = None,
    existing_created_by: str | None = None,
) -> tuple[WorkflowService, SimpleNamespace]:
    service = WorkflowService()
    existing_workflow = SimpleNamespace(
        version=2,
        cdp_connect_headers=None,
        workflow_permanent_id="wpid_test",
        folder_id=None,
        code_version=None,
        max_elapsed_time_minutes=existing_max_elapsed_time_minutes,
        enable_self_healing=existing_enable_self_healing,
        pin_saved_session_ip=existing_pin_saved_session_ip,
        webhook_callback_url=existing_webhook_callback_url,
        created_by=existing_created_by,
    )
    potential_workflow = SimpleNamespace(workflow_id="wf_new")
    updated_workflow = SimpleNamespace(workflow_id="wf_new", workflow_permanent_id="wpid_test")

    service.get_workflow_by_permanent_id = AsyncMock(return_value=existing_workflow)  # type: ignore[method-assign]
    service.create_workflow = AsyncMock(return_value=potential_workflow)  # type: ignore[method-assign]
    service.make_workflow_definition = AsyncMock(  # type: ignore[method-assign]
        return_value=WorkflowDefinition(parameters=[], blocks=[])
    )
    service.validate_workflow_block_graph = Mock()  # type: ignore[method-assign]
    service._validate_payload_templates = Mock()  # type: ignore[method-assign]
    service.update_workflow_definition = AsyncMock(return_value=updated_workflow)  # type: ignore[method-assign]
    service.maybe_delete_cached_code = AsyncMock()  # type: ignore[method-assign]
    service._refresh_workflow_schedule_runtime_limits = AsyncMock()  # type: ignore[method-assign]

    return service, updated_workflow


def _make_setup_service(workflow: SimpleNamespace) -> tuple[WorkflowService, SimpleNamespace, SimpleNamespace]:
    service = WorkflowService()
    workflow_run = SimpleNamespace(
        workflow_run_id="wr_test",
        workflow_permanent_id="wpid_test",
        organization_id="org_test",
        browser_profile_id=None,
        browser_seed_source=None,
        proxy_location=None,
    )

    service.get_workflow_by_permanent_id = AsyncMock(return_value=workflow)  # type: ignore[method-assign]
    service.create_workflow_run = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]
    service.get_workflow_parameters = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service.create_workflow_run_parameters = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service.mark_workflow_run_as_failed = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]
    # These tests assert the request-level browser_profile_id copy, not seed resolution.
    service._resolve_and_stamp_run_seed = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]

    organization = SimpleNamespace(
        organization_id="org_test",
        organization_name="Test Org",
        default_llm_key=None,
        default_secondary_llm_key=None,
    )
    return service, organization, workflow_run


def _configure_setup_app_mocks(mock_app: Any) -> None:
    mock_app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=None)
    mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
    mock_app.AGENT_FUNCTION.should_use_flex_llm_routing = AsyncMock(return_value=False)
    mock_app.DATABASE.workflow_runs.update_workflow_run = AsyncMock()


def _make_workflow_stub(
    browser_profile_id: str | None,
    max_elapsed_time_minutes: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        organization_id="org_test",
        title="test",
        proxy_location=None,
        webhook_callback_url=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_profile_id=browser_profile_id,
        browser_profile_key=None,
        persist_browser_session=False,
        pin_saved_session_ip=False,
        max_elapsed_time_minutes=max_elapsed_time_minutes,
        run_with="agent",
        code_version=None,
        adaptive_caching=False,
        sequential_key=None,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
    )


@pytest.fixture(autouse=True)
def reset_context() -> Generator[None]:
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
        _configure_setup_app_mocks(mock_app)

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
        _configure_setup_app_mocks(mock_app)

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
        _configure_setup_app_mocks(mock_app)

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
        _configure_setup_app_mocks(mock_app)

        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=request,
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

    assert request.browser_profile_id is None


@pytest.mark.asyncio
async def test_setup_workflow_run_prefers_request_max_elapsed_time_over_workflow_default() -> None:
    """A run-level runtime cap should override the workflow default for that run snapshot."""
    workflow_stub = _make_workflow_stub(browser_profile_id=None, max_elapsed_time_minutes=120)
    service, organization, _ = _make_setup_service(workflow_stub)

    request = WorkflowRequestBody(data={}, max_elapsed_time_minutes=10)

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        _configure_setup_app_mocks(mock_app)

        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=request,
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

    create_workflow_run_mock = service.create_workflow_run
    assert isinstance(create_workflow_run_mock, AsyncMock)
    create_workflow_run_mock.assert_awaited_once()
    assert create_workflow_run_mock.await_args is not None
    assert create_workflow_run_mock.await_args.kwargs["max_elapsed_time_minutes"] == 10


@pytest.mark.asyncio
async def test_setup_workflow_run_without_credentials_writes_no_sequential_credential() -> None:
    """A credential-less run leaves sequential_credential_id NULL — the MVP never writes a completion
    sentinel. Readiness is proven from queued status + queued task + parameter equality, so no credential
    write happens and the column stays NULL for a run that resolves to no sequential credential."""
    workflow_stub = _make_workflow_stub(browser_profile_id=None)
    service, organization, _ = _make_setup_service(workflow_stub)

    request = WorkflowRequestBody(data={})

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        _configure_setup_app_mocks(mock_app)

        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=request,
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

        update_mock = mock_app.DATABASE.workflow_runs.update_workflow_run
        assert all("sequential_credential_id" not in call.kwargs for call in update_mock.await_args_list)

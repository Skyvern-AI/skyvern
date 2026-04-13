"""Tests for WorkflowService.setup_workflow_run batch parameter persistence.

Verifies that setup_workflow_run collects all parameter values first and
persists them in a single batch insert, and that validation failures
(missing params, invalid credentials, DB errors) are handled correctly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from skyvern.exceptions import InvalidCredentialId, MissingValueForParameter, WorkflowRunParameterPersistenceError
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
from skyvern.forge.sdk.workflow.service import WorkflowService


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


def _make_service_with_mocks(
    *,
    workflow_parameters: list[WorkflowParameter],
    batch_side_effect: Exception | None = None,
    single_side_effect: Exception | None = None,
) -> tuple[WorkflowService, SimpleNamespace, SimpleNamespace]:
    """Helper to build a WorkflowService with mocked internals for setup_workflow_run tests."""
    service = WorkflowService()
    workflow = SimpleNamespace(
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        organization_id="org_test",
        proxy_location=None,
        webhook_callback_url=None,
        extra_http_headers=None,
        run_with="agent",
        code_version=None,
        adaptive_caching=False,
        sequential_key=None,
    )
    workflow_run = SimpleNamespace(workflow_run_id="wr_test", workflow_permanent_id="wpid_test")

    service.get_workflow_by_permanent_id = AsyncMock(return_value=workflow)  # type: ignore[method-assign]
    service.create_workflow_run = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]
    service.get_workflow_parameters = AsyncMock(return_value=workflow_parameters)  # type: ignore[method-assign]
    if batch_side_effect:
        service.create_workflow_run_parameters = AsyncMock(side_effect=batch_side_effect)  # type: ignore[method-assign]
    else:
        service.create_workflow_run_parameters = AsyncMock(return_value=[])  # type: ignore[method-assign]
    if single_side_effect:
        service.create_workflow_run_parameter = AsyncMock(side_effect=single_side_effect)  # type: ignore[method-assign]
    else:
        service.create_workflow_run_parameter = AsyncMock()  # type: ignore[method-assign]
    service.mark_workflow_run_as_failed = AsyncMock(return_value=workflow_run)  # type: ignore[method-assign]

    organization = SimpleNamespace(organization_id="org_test", organization_name="Test Org")
    return service, organization, workflow_run


@pytest.fixture(autouse=True)
def reset_context() -> None:
    skyvern_context.reset()
    yield
    skyvern_context.reset()


@pytest.mark.asyncio
async def test_setup_workflow_run_raises_on_missing_required_parameters() -> None:
    """When required parameters have no value and no default, setup should raise MissingValueForParameter."""
    required_param = _make_workflow_parameter("api_key")  # no default_value
    service, organization, _ = _make_service_with_mocks(workflow_parameters=[required_param])

    request = WorkflowRequestBody(data={})  # no data for api_key

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        with pytest.raises(MissingValueForParameter):
            await service.setup_workflow_run(
                request_id="req_test",
                workflow_request=request,
                workflow_permanent_id="wpid_test",
                organization=organization,
            )

    service.create_workflow_run_parameters.assert_not_awaited()
    service.mark_workflow_run_as_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_workflow_run_persistence_error_identifies_specific_failing_parameter() -> None:
    """When batch fails with multiple params, fallback to one-by-one should pinpoint the failing key."""
    params = [
        _make_workflow_parameter(
            "alpha_count", workflow_parameter_type=WorkflowParameterType.INTEGER, default_value="1"
        ),
        _make_workflow_parameter("middle_label", default_value="mid"),
        _make_workflow_parameter("zebra_url", default_value="https://zebra.example.com"),
    ]
    batch_error = IntegrityError("INSERT", {}, Exception("constraint failed"))
    single_error = IntegrityError("INSERT", {}, Exception("NOT NULL constraint on middle_label"))

    # Single insert succeeds for alpha_count, fails on middle_label
    async def _single_insert_side_effect(
        *, workflow_run_id: str, workflow_parameter: WorkflowParameter, value: object
    ) -> None:
        if workflow_parameter.key == "middle_label":
            raise single_error

    service, organization, _ = _make_service_with_mocks(
        workflow_parameters=params,
        batch_side_effect=batch_error,
    )
    service.create_workflow_run_parameter = AsyncMock(side_effect=_single_insert_side_effect)  # type: ignore[method-assign]

    request = WorkflowRequestBody(data={"alpha_count": 5, "middle_label": "test", "zebra_url": "https://z.com"})

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        with pytest.raises(WorkflowRunParameterPersistenceError) as exc_info:
            await service.setup_workflow_run(
                request_id="req_test",
                workflow_request=request,
                workflow_permanent_id="wpid_test",
                organization=organization,
            )

    error_message = str(exc_info.value)
    # Should identify only the failing parameter, not all three
    assert "middle_label" in error_message
    assert "alpha_count" not in error_message
    assert "zebra_url" not in error_message
    assert exc_info.value.__cause__ is single_error


@pytest.mark.asyncio
async def test_setup_workflow_run_raises_on_non_string_credential_id() -> None:
    """Credential ID parameters must be strings. Passing an int should raise InvalidCredentialId."""
    cred_param = _make_workflow_parameter(
        "credential",
        workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
    )
    service, organization, _ = _make_service_with_mocks(workflow_parameters=[cred_param])

    request = WorkflowRequestBody(data={"credential": 12345})  # not a string

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        with pytest.raises(InvalidCredentialId):
            await service.setup_workflow_run(
                request_id="req_test",
                workflow_request=request,
                workflow_permanent_id="wpid_test",
                organization=organization,
            )

    service.create_workflow_run_parameters.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_workflow_run_preserves_parent_loop_state_when_replacing_context() -> None:
    service, organization, _ = _make_service_with_mocks(workflow_parameters=[])

    loop_state = {"downloaded_file_signatures_before_iteration": [("a.pdf", "abc", "https://files/a.pdf")]}
    parent_context = SkyvernContext(
        organization_id="org_test",
        organization_name="Test Org",
        workflow_run_id="wr_parent",
        root_workflow_run_id="wr_root",
        run_id="wr_parent",
        loop_internal_state=loop_state,
    )
    skyvern_context.set(parent_context)

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

        await service.setup_workflow_run(
            request_id="req_test",
            workflow_request=WorkflowRequestBody(data={}),
            workflow_permanent_id="wpid_test",
            organization=organization,
        )

    current_context = skyvern_context.current()
    assert current_context is not None
    assert current_context.workflow_run_id == "wr_test"
    assert current_context.run_id == "wr_parent"
    assert current_context.root_workflow_run_id == "wr_root"
    assert current_context.loop_internal_state == loop_state
    assert current_context.loop_internal_state is not loop_state

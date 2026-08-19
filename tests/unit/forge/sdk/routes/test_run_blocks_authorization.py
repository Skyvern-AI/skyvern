from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException, status

from skyvern.exceptions import RateLimitExceeded
from skyvern.forge.sdk.routes import run_blocks as run_blocks_mod
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.tags import CallerType
from skyvern.schemas.credential_type import CredentialType
from skyvern.schemas.run_blocks import DownloadFilesRequest, LoginRequest


def _workflow_stub() -> SimpleNamespace:
    return SimpleNamespace(
        title="Generated workflow",
        description=None,
        status="auto_generated",
        workflow_permanent_id="wpid_test",
        workflow_id="wf_test",
    )


def _app_mock() -> MagicMock:
    app_mock = MagicMock()
    app_mock.WORKFLOW_SERVICE.create_empty_workflow = AsyncMock(return_value=_workflow_stub())
    app_mock.WORKFLOW_SERVICE.create_workflow_from_request = AsyncMock(return_value=_workflow_stub())
    app_mock.DATABASE.credentials.get_credential = AsyncMock(return_value=SimpleNamespace(totp_identifier=None))
    app_mock.RATE_LIMITER.rate_limit_submit_run = AsyncMock()
    return app_mock


def _login_request() -> LoginRequest:
    return LoginRequest(
        url="https://example.com",
        credential_type=CredentialType.skyvern,
        credential_id="cred_test",
        browser_session_id="pbs_123",
    )


def _download_files_request() -> DownloadFilesRequest:
    return DownloadFilesRequest(
        url="https://example.com",
        navigation_goal="Download the statement.",
        browser_session_id="pbs_123",
    )


ENDPOINTS = [
    ("login", "login_request", _login_request),
    ("download_files", "download_files_request", _download_files_request),
]


def _caller(organization: SimpleNamespace) -> org_auth_service.CallerContext:
    return org_auth_service.CallerContext(
        organization=organization,
        caller_id=organization.organization_id,
        caller_type=CallerType.API_KEY,
    )


async def _invoke(handler_name: str, request_kwarg: str, request_obj: Any, organization: SimpleNamespace) -> Any:
    handler = getattr(run_blocks_mod, handler_name)
    return await handler(
        request=MagicMock(),
        background_tasks=BackgroundTasks(),
        caller=_caller(organization),
        **{request_kwarg: request_obj},
    )


@pytest.mark.parametrize(("handler_name", "request_kwarg", "request_factory"), ENDPOINTS)
@pytest.mark.asyncio
async def test_task_endpoint_refuses_credit_exhausted_org(
    handler_name: str, request_kwarg: str, request_factory: Callable[[], Any]
) -> None:
    app_mock = _app_mock()
    organization = SimpleNamespace(organization_id="org_test")
    permission_checker = SimpleNamespace(
        check=AsyncMock(
            side_effect=HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Credits exhausted. Enable overage or upgrade your plan.",
            )
        )
    )

    with (
        patch.object(run_blocks_mod, "app", app_mock),
        patch.object(run_blocks_mod.PermissionCheckerFactory, "get_instance", lambda: permission_checker),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _invoke(handler_name, request_kwarg, request_factory(), organization)

    assert exc_info.value.status_code == status.HTTP_402_PAYMENT_REQUIRED
    permission_checker.check.assert_awaited_once_with(organization, browser_session_id="pbs_123")
    app_mock.RATE_LIMITER.rate_limit_submit_run.assert_not_awaited()
    app_mock.WORKFLOW_SERVICE.create_empty_workflow.assert_not_awaited()


@pytest.mark.parametrize(("handler_name", "request_kwarg", "request_factory"), ENDPOINTS)
@pytest.mark.asyncio
async def test_task_endpoint_enforces_rate_limit(
    handler_name: str, request_kwarg: str, request_factory: Callable[[], Any]
) -> None:
    app_mock = _app_mock()
    app_mock.RATE_LIMITER.rate_limit_submit_run = AsyncMock(
        side_effect=RateLimitExceeded(organization_id="org_test", max_requests=5, window_seconds=60)
    )
    organization = SimpleNamespace(organization_id="org_test")
    permission_checker = SimpleNamespace(check=AsyncMock())

    with (
        patch.object(run_blocks_mod, "app", app_mock),
        patch.object(run_blocks_mod.PermissionCheckerFactory, "get_instance", lambda: permission_checker),
    ):
        with pytest.raises(RateLimitExceeded):
            await _invoke(handler_name, request_kwarg, request_factory(), organization)

    app_mock.RATE_LIMITER.rate_limit_submit_run.assert_awaited_once_with("org_test")
    app_mock.WORKFLOW_SERVICE.create_empty_workflow.assert_not_awaited()

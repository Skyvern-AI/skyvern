from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.runs import ProxyLocation


def _credential_parameter(credential_id: str = "cred_1") -> CredentialParameter:
    now = datetime.now(timezone.utc)
    return CredentialParameter(
        key="login",
        credential_parameter_id="cp_1",
        workflow_id="wf_1",
        credential_id=credential_id,
        created_at=now,
        modified_at=now,
    )


def _proxy_header_hook() -> SimpleNamespace:
    return SimpleNamespace(
        has_proxy_session_extra_http_headers=lambda headers: bool(headers and "dedicated-ip" in headers),
        merge_proxy_session_extra_http_headers=lambda headers, proxy_session_id: {
            **dict(headers or {}),
            "dedicated-ip": proxy_session_id,
        },
    )


@pytest.mark.asyncio
async def test_login_block_credential_proxy_pin_applies_to_workflow_run() -> None:
    service = WorkflowService()
    workflow_run = SimpleNamespace(workflow_run_id="wr_1", extra_http_headers={"X-Test": "1"}, proxy_location=None)
    block = SimpleNamespace(parameters=[_credential_parameter()])
    credential = SimpleNamespace(proxy_session_id="credential-pin", proxy_location=ProxyLocation.RESIDENTIAL_ISP)

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.AGENT_FUNCTION = _proxy_header_hook()
        mock_app.DATABASE.credentials.get_credential = AsyncMock(return_value=credential)
        mock_app.DATABASE.workflow_runs.update_workflow_run = AsyncMock()

        await service._apply_login_block_credential_proxy_pin(
            block=block,  # type: ignore[arg-type]
            workflow_run=workflow_run,  # type: ignore[arg-type]
            workflow_run_id="wr_1",
            organization_id="org_1",
        )

    assert workflow_run.extra_http_headers == {"X-Test": "1", "dedicated-ip": "credential-pin"}
    assert workflow_run.proxy_location == ProxyLocation.RESIDENTIAL_ISP
    mock_app.DATABASE.workflow_runs.update_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_1",
        extra_http_headers={"X-Test": "1", "dedicated-ip": "credential-pin"},
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
    )


@pytest.mark.asyncio
async def test_login_block_credential_proxy_pin_respects_existing_run_header() -> None:
    service = WorkflowService()
    workflow_run = SimpleNamespace(
        workflow_run_id="wr_1", extra_http_headers={"dedicated-ip": "run-pin"}, proxy_location=None
    )
    block = SimpleNamespace(parameters=[_credential_parameter()])

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.AGENT_FUNCTION = _proxy_header_hook()
        mock_app.DATABASE.credentials.get_credential = AsyncMock()
        mock_app.DATABASE.workflow_runs.update_workflow_run = AsyncMock()

        await service._apply_login_block_credential_proxy_pin(
            block=block,  # type: ignore[arg-type]
            workflow_run=workflow_run,  # type: ignore[arg-type]
            workflow_run_id="wr_1",
            organization_id="org_1",
        )

    mock_app.DATABASE.credentials.get_credential.assert_not_called()
    mock_app.DATABASE.workflow_runs.update_workflow_run.assert_not_called()
    assert workflow_run.extra_http_headers == {"dedicated-ip": "run-pin"}


@pytest.mark.asyncio
async def test_login_block_credential_proxy_pin_preserves_run_proxy_location() -> None:
    service = WorkflowService()
    workflow_run = SimpleNamespace(
        workflow_run_id="wr_1", extra_http_headers=None, proxy_location=ProxyLocation.RESIDENTIAL
    )
    block = SimpleNamespace(parameters=[_credential_parameter()])
    credential = SimpleNamespace(proxy_session_id="credential-pin", proxy_location=ProxyLocation.RESIDENTIAL_ISP)

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.AGENT_FUNCTION = _proxy_header_hook()
        mock_app.DATABASE.credentials.get_credential = AsyncMock(return_value=credential)
        mock_app.DATABASE.workflow_runs.update_workflow_run = AsyncMock()

        await service._apply_login_block_credential_proxy_pin(
            block=block,  # type: ignore[arg-type]
            workflow_run=workflow_run,  # type: ignore[arg-type]
            workflow_run_id="wr_1",
            organization_id="org_1",
        )

    update_kwargs: dict[str, Any] = mock_app.DATABASE.workflow_runs.update_workflow_run.await_args.kwargs
    assert update_kwargs == {
        "workflow_run_id": "wr_1",
        "extra_http_headers": {"dedicated-ip": "credential-pin"},
    }
    assert workflow_run.proxy_location == ProxyLocation.RESIDENTIAL


@pytest.mark.asyncio
async def test_login_block_credential_proxy_pin_prefers_linked_profile_pin() -> None:
    service = WorkflowService()
    workflow_run = SimpleNamespace(workflow_run_id="wr_1", extra_http_headers=None, proxy_location=None)
    block = SimpleNamespace(parameters=[_credential_parameter()])
    credential = SimpleNamespace(
        proxy_session_id="credential-pin",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        browser_profile_id="bp_1",
    )
    profile = SimpleNamespace(
        proxy_session_id="profile-pin",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
    )

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.AGENT_FUNCTION = _proxy_header_hook()
        mock_app.DATABASE.credentials.get_credential = AsyncMock(return_value=credential)
        mock_app.DATABASE.browser_sessions.get_browser_profile = AsyncMock(return_value=profile)
        mock_app.DATABASE.workflow_runs.update_workflow_run = AsyncMock()

        await service._apply_login_block_credential_proxy_pin(
            block=block,  # type: ignore[arg-type]
            workflow_run=workflow_run,  # type: ignore[arg-type]
            workflow_run_id="wr_1",
            organization_id="org_1",
        )

    assert workflow_run.extra_http_headers == {"dedicated-ip": "profile-pin"}
    assert workflow_run.proxy_location == ProxyLocation.RESIDENTIAL_ISP
    mock_app.DATABASE.workflow_runs.update_workflow_run.assert_awaited_once_with(
        workflow_run_id="wr_1",
        extra_http_headers={"dedicated-ip": "profile-pin"},
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
    )


@pytest.mark.asyncio
async def test_login_block_credential_proxy_pin_skips_when_linked_profile_is_unpinned() -> None:
    service = WorkflowService()
    workflow_run = SimpleNamespace(workflow_run_id="wr_1", extra_http_headers=None, proxy_location=None)
    block = SimpleNamespace(parameters=[_credential_parameter()])
    credential = SimpleNamespace(
        proxy_session_id="credential-pin",
        proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        browser_profile_id="bp_1",
    )
    profile = SimpleNamespace(proxy_session_id=None, proxy_location=None)

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.AGENT_FUNCTION = _proxy_header_hook()
        mock_app.DATABASE.credentials.get_credential = AsyncMock(return_value=credential)
        mock_app.DATABASE.browser_sessions.get_browser_profile = AsyncMock(return_value=profile)
        mock_app.DATABASE.workflow_runs.update_workflow_run = AsyncMock()

        await service._apply_login_block_credential_proxy_pin(
            block=block,  # type: ignore[arg-type]
            workflow_run=workflow_run,  # type: ignore[arg-type]
            workflow_run_id="wr_1",
            organization_id="org_1",
        )

    assert workflow_run.extra_http_headers is None
    assert workflow_run.proxy_location is None
    mock_app.DATABASE.workflow_runs.update_workflow_run.assert_not_called()


@pytest.mark.asyncio
async def test_login_block_credential_proxy_pin_does_not_mutate_run_when_db_update_fails() -> None:
    service = WorkflowService()
    workflow_run = SimpleNamespace(workflow_run_id="wr_1", extra_http_headers=None, proxy_location=None)
    block = SimpleNamespace(parameters=[_credential_parameter()])
    credential = SimpleNamespace(proxy_session_id="credential-pin", proxy_location=ProxyLocation.RESIDENTIAL_ISP)

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.AGENT_FUNCTION = _proxy_header_hook()
        mock_app.DATABASE.credentials.get_credential = AsyncMock(return_value=credential)
        mock_app.DATABASE.workflow_runs.update_workflow_run = AsyncMock(side_effect=RuntimeError("db down"))

        await service._apply_login_block_credential_proxy_pin(
            block=block,  # type: ignore[arg-type]
            workflow_run=workflow_run,  # type: ignore[arg-type]
            workflow_run_id="wr_1",
            organization_id="org_1",
        )

    assert workflow_run.extra_http_headers is None
    assert workflow_run.proxy_location is None

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks

from skyvern.config import settings
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.routes import run_blocks as run_blocks_mod
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.tags import CallerType
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.credential_type import CredentialType
from skyvern.schemas.run_blocks import DownloadFilesRequest, LoginRequest
from skyvern.schemas.runs import ProxyLocation


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


def _caller() -> org_auth_service.CallerContext:
    organization = SimpleNamespace(organization_id="org_test")
    return org_auth_service.CallerContext(
        organization=organization,
        caller_id=organization.organization_id,
        caller_type=CallerType.API_KEY,
    )


@pytest.mark.parametrize(
    ("rollout_enabled", "input_proxy_location", "expected_proxy_location"),
    [
        (False, None, ProxyLocation.RESIDENTIAL),
        (True, None, ProxyLocation.NONE),
        (False, ProxyLocation.RESIDENTIAL_GB, ProxyLocation.RESIDENTIAL_GB),
        (False, ProxyLocation.NONE, ProxyLocation.NONE),
    ],
)
@pytest.mark.asyncio
async def test_login_generated_workflow_uses_runtime_proxy_default(
    monkeypatch: pytest.MonkeyPatch,
    rollout_enabled: bool,
    input_proxy_location: ProxyLocation | None,
    expected_proxy_location: ProxyLocation,
) -> None:
    monkeypatch.setattr(settings, "RUNTIME_PROXY_DEFAULT_NONE_ENABLED", rollout_enabled)
    app_mock = _app_mock()
    response = SimpleNamespace(run_id="wr_test")
    run_response = AsyncMock(return_value=response)

    with (
        patch.object(run_blocks_mod, "app", app_mock),
        patch.object(run_blocks_mod, "_run_workflow_and_build_response", run_response),
    ):
        result = await run_blocks_mod.login(
            request=MagicMock(),
            background_tasks=BackgroundTasks(),
            login_request=LoginRequest(
                url="https://example.com",
                credential_type=CredentialType.skyvern,
                credential_id="cred_test",
                proxy_location=input_proxy_location,
            ),
            caller=_caller(),
        )

    assert result is response
    workflow_request = app_mock.WORKFLOW_SERVICE.create_workflow_from_request.await_args.kwargs["request"]
    assert workflow_request.proxy_location == expected_proxy_location


@pytest.mark.parametrize(
    ("rollout_enabled", "input_proxy_location", "expected_proxy_location"),
    [
        (False, None, ProxyLocation.RESIDENTIAL),
        (True, None, ProxyLocation.NONE),
        (False, ProxyLocation.RESIDENTIAL_GB, ProxyLocation.RESIDENTIAL_GB),
        (False, ProxyLocation.NONE, ProxyLocation.NONE),
    ],
)
@pytest.mark.asyncio
async def test_download_files_generated_workflow_uses_runtime_proxy_default(
    monkeypatch: pytest.MonkeyPatch,
    rollout_enabled: bool,
    input_proxy_location: ProxyLocation | None,
    expected_proxy_location: ProxyLocation,
) -> None:
    monkeypatch.setattr(settings, "RUNTIME_PROXY_DEFAULT_NONE_ENABLED", rollout_enabled)
    app_mock = _app_mock()
    response = SimpleNamespace(run_id="wr_test")
    run_response = AsyncMock(return_value=response)

    with (
        patch.object(run_blocks_mod, "app", app_mock),
        patch.object(run_blocks_mod, "_run_workflow_and_build_response", run_response),
    ):
        result = await run_blocks_mod.download_files(
            request=MagicMock(),
            background_tasks=BackgroundTasks(),
            download_files_request=DownloadFilesRequest(
                url="https://example.com",
                navigation_goal="Download the statement.",
                proxy_location=input_proxy_location,
            ),
            caller=_caller(),
        )

    assert result is response
    workflow_request = app_mock.WORKFLOW_SERVICE.create_workflow_from_request.await_args.kwargs["request"]
    assert workflow_request.proxy_location == expected_proxy_location


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["login", "download_files"])
async def test_generated_workflow_persists_no_run_headers(monkeypatch, sqlite_engine, endpoint) -> None:
    database = AgentDB("", db_engine=sqlite_engine)
    service = WorkflowService()
    organization = await database.organizations.create_organization("Header fixture")
    monkeypatch.setattr(run_blocks_mod.app, "DATABASE", database)
    monkeypatch.setattr(run_blocks_mod.app, "WORKFLOW_SERVICE", service)
    monkeypatch.setattr(run_blocks_mod.app, "AGENT_FUNCTION", AgentFunction())
    monkeypatch.setattr(
        run_blocks_mod,
        "app",
        SimpleNamespace(
            DATABASE=database,
            WORKFLOW_SERVICE=service,
            RATE_LIMITER=SimpleNamespace(rate_limit_submit_run=AsyncMock()),
        ),
    )
    monkeypatch.setattr(
        database.credentials, "get_credential", AsyncMock(return_value=SimpleNamespace(totp_identifier=None))
    )
    run_response = AsyncMock(return_value=SimpleNamespace(run_id="wr_test"))
    monkeypatch.setattr(run_blocks_mod, "_run_workflow_and_build_response", run_response)
    caller = org_auth_service.CallerContext(
        organization=organization, caller_id=organization.organization_id, caller_type=CallerType.API_KEY
    )
    headers = {"X-Run-Only": "synthetic-run-value"}
    if endpoint == "login":
        await run_blocks_mod.login(
            request=MagicMock(),
            background_tasks=BackgroundTasks(),
            login_request=LoginRequest(
                url="https://example.test",
                credential_type=CredentialType.skyvern,
                credential_id="cred_test",
                extra_http_headers=headers,
            ),
            caller=caller,
        )
    else:
        await run_blocks_mod.download_files(
            request=MagicMock(),
            background_tasks=BackgroundTasks(),
            download_files_request=DownloadFilesRequest(
                url="https://example.test", navigation_goal="Download the file", extra_http_headers=headers
            ),
            caller=caller,
        )
    run_args = run_response.await_args.kwargs
    saved = await database.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=run_args["workflow_id"], organization_id=organization.organization_id
    )
    blank = await database.workflows.get_workflow(
        workflow_id=run_args["new_workflow"].workflow_id, organization_id=organization.organization_id
    )
    assert saved is not None and blank is not None
    assert not blank.extra_http_headers and not blank.cdp_connect_headers
    assert saved.extra_http_headers == {} and saved.cdp_connect_headers == {}
    assert saved.workflow_definition.blocks
    assert run_args["run_block_request"].extra_http_headers == headers

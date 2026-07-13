from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks

from skyvern.config import settings
from skyvern.forge.sdk.routes import run_blocks as run_blocks_mod
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
    return app_mock


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
            organization=SimpleNamespace(organization_id="org_test"),
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
            organization=SimpleNamespace(organization_id="org_test"),
        )

    assert result is response
    workflow_request = app_mock.WORKFLOW_SERVICE.create_workflow_from_request.await_args.kwargs["request"]
    assert workflow_request.proxy_location == expected_proxy_location

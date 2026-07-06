import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient

from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.routes import credentials
from skyvern.forge.sdk.schemas.credentials import CredentialType
from skyvern.forge.sdk.schemas.credentials import TestCredentialRequest as CredentialTestRequest
from skyvern.forge.sdk.schemas.credentials import TestLoginRequest as LoginTestRequest
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.runs import ProxyLocation

USER_AGENT_CASES = [
    ("skyvern-ui", WorkflowRunTriggerType.manual),
    ("skyvern-mcp", WorkflowRunTriggerType.manual),
    (None, WorkflowRunTriggerType.api),
    ("python-sdk/1.0", WorkflowRunTriggerType.api),
]


def _workflow_service(setup_mock: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(
        create_workflow_from_request=AsyncMock(return_value=SimpleNamespace(workflow_permanent_id="wpid_test")),
        setup_workflow_run=setup_mock,
    )


def _setup_mock() -> AsyncMock:
    return AsyncMock(
        return_value=SimpleNamespace(
            workflow_run_id="wr_test",
            workflow_id="wf_test",
            workflow_permanent_id="wpid_test",
        )
    )


def _patch_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = SimpleNamespace(execute_workflow=AsyncMock())
    monkeypatch.setattr(credentials.AsyncExecutorFactory, "get_executor", lambda: executor)


def _patch_cloud_proxy_session_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_function = SimpleNamespace(
        build_proxy_session_extra_http_headers=lambda proxy_session_id: (
            {"dedicated-ip": proxy_session_id} if proxy_session_id else None
        )
    )
    monkeypatch.setattr(credentials.app, "AGENT_FUNCTION", agent_function)


@pytest.mark.parametrize("x_user_agent, expected", USER_AGENT_CASES)
@pytest.mark.asyncio
async def test_test_credential_trigger_type_from_user_agent(
    monkeypatch: pytest.MonkeyPatch,
    x_user_agent: str | None,
    expected: WorkflowRunTriggerType,
) -> None:
    setup_mock = _setup_mock()
    mock_credentials = SimpleNamespace(
        get_credential=AsyncMock(
            return_value=SimpleNamespace(
                credential_type=CredentialType.PASSWORD,
                browser_profile_id=None,
                totp_identifier=None,
                name="Test Credential",
                proxy_location=None,
                proxy_session_id=None,
            )
        )
    )
    monkeypatch.setattr(credentials.app, "DATABASE", SimpleNamespace(credentials=mock_credentials))
    monkeypatch.setattr(credentials.app, "WORKFLOW_SERVICE", _workflow_service(setup_mock))
    _patch_cloud_proxy_session_headers(monkeypatch)
    _patch_executor(monkeypatch)

    response = await credentials.test_credential(
        background_tasks=BackgroundTasks(),
        credential_id="cred_test",
        data=CredentialTestRequest(url="https://example.com/login", save_browser_profile=False),
        current_org=SimpleNamespace(organization_id="org_test"),
        x_user_agent=x_user_agent,
    )

    setup_mock.assert_awaited_once()
    assert setup_mock.call_args.kwargs["trigger_type"] == expected
    assert response.workflow_run_id == "wr_test"


@pytest.mark.asyncio
async def test_test_credential_seeds_proxy_pin_workflow_request(monkeypatch: pytest.MonkeyPatch) -> None:
    setup_mock = _setup_mock()
    mock_credentials = SimpleNamespace(
        get_credential=AsyncMock(
            return_value=SimpleNamespace(
                credential_type=CredentialType.PASSWORD,
                browser_profile_id=None,
                totp_identifier=None,
                name="Test Credential",
                proxy_location=ProxyLocation.RESIDENTIAL_ISP,
                proxy_session_id="abc1234567",
            )
        )
    )
    monkeypatch.setattr(credentials.app, "DATABASE", SimpleNamespace(credentials=mock_credentials))
    monkeypatch.setattr(credentials.app, "WORKFLOW_SERVICE", _workflow_service(setup_mock))
    _patch_cloud_proxy_session_headers(monkeypatch)
    _patch_executor(monkeypatch)

    await credentials.test_credential(
        background_tasks=BackgroundTasks(),
        credential_id="cred_test",
        data=CredentialTestRequest(url="https://example.com/login", save_browser_profile=False),
        current_org=SimpleNamespace(organization_id="org_test"),
        x_user_agent=None,
    )

    workflow_request = setup_mock.call_args.kwargs["workflow_request"]
    assert workflow_request.proxy_location == ProxyLocation.RESIDENTIAL_ISP
    assert workflow_request.extra_http_headers == {"dedicated-ip": "abc1234567"}


@pytest.mark.parametrize("x_user_agent, expected", USER_AGENT_CASES)
@pytest.mark.asyncio
async def test_test_login_trigger_type_from_user_agent(
    monkeypatch: pytest.MonkeyPatch,
    x_user_agent: str | None,
    expected: WorkflowRunTriggerType,
) -> None:
    setup_mock = _setup_mock()
    vault_service = SimpleNamespace(
        create_credential=AsyncMock(
            return_value=SimpleNamespace(vault_type=None, credential_id="cred_test", totp_identifier=None)
        )
    )
    monkeypatch.setattr(credentials, "_get_credential_vault_service", AsyncMock(return_value=vault_service))
    monkeypatch.setattr(credentials, "_create_browser_profile_after_workflow", AsyncMock())
    monkeypatch.setattr(credentials.app, "WORKFLOW_SERVICE", _workflow_service(setup_mock))
    _patch_executor(monkeypatch)

    response = await credentials.test_login(
        background_tasks=BackgroundTasks(),
        data=LoginTestRequest(url="https://example.com/login", username="user@example.com", password="pw"),
        current_org=SimpleNamespace(organization_id="org_test"),
        x_user_agent=x_user_agent,
    )

    setup_mock.assert_awaited_once()
    assert setup_mock.call_args.kwargs["trigger_type"] == expected
    assert response.workflow_run_id == "wr_test"
    # test_login fires _create_browser_profile_after_workflow via create_task; drain the
    # stubbed task so the loop doesn't warn about a pending task at teardown.
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_test_login_seeds_proxy_pin_workflow_request(monkeypatch: pytest.MonkeyPatch) -> None:
    setup_mock = _setup_mock()
    vault_service = SimpleNamespace(
        create_credential=AsyncMock(
            return_value=SimpleNamespace(vault_type=None, credential_id="cred_test", totp_identifier=None)
        )
    )
    mock_credentials = SimpleNamespace(
        update_credential=AsyncMock(
            return_value=SimpleNamespace(
                vault_type=None,
                credential_id="cred_test",
                totp_identifier=None,
                proxy_location=ProxyLocation.RESIDENTIAL_ISP,
                proxy_session_id="abc1234567",
            )
        )
    )
    monkeypatch.setattr(credentials, "_get_credential_vault_service", AsyncMock(return_value=vault_service))
    monkeypatch.setattr(credentials, "_create_browser_profile_after_workflow", AsyncMock())
    monkeypatch.setattr(credentials.app, "DATABASE", SimpleNamespace(credentials=mock_credentials))
    monkeypatch.setattr(credentials.app, "WORKFLOW_SERVICE", _workflow_service(setup_mock))
    _patch_cloud_proxy_session_headers(monkeypatch)
    _patch_executor(monkeypatch)

    await credentials.test_login(
        background_tasks=BackgroundTasks(),
        data=LoginTestRequest(
            url="https://example.com/login",
            username="user@example.com",
            password="pw",
            proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        ),
        current_org=SimpleNamespace(organization_id="org_test"),
        x_user_agent=None,
    )

    workflow_request = setup_mock.call_args.kwargs["workflow_request"]
    assert workflow_request.proxy_location == ProxyLocation.RESIDENTIAL_ISP
    assert workflow_request.extra_http_headers == {"dedicated-ip": "abc1234567"}
    await asyncio.sleep(0)


def _auth_override() -> SimpleNamespace:
    return SimpleNamespace(organization_id="org_test")


# The tests above call the handlers directly, bypassing FastAPI's Header() injection; these
# drive the real HTTP path so a regression in the x-user-agent -> x_user_agent wiring is caught.
# Mixed header casing asserts the lookup stays case-insensitive.
@pytest.mark.parametrize(
    "headers, expected",
    [
        ({"x-user-agent": "skyvern-ui"}, WorkflowRunTriggerType.manual),
        ({"X-User-Agent": "skyvern-mcp"}, WorkflowRunTriggerType.manual),
        ({}, WorkflowRunTriggerType.api),
        ({"x-user-agent": "python-sdk/1.0"}, WorkflowRunTriggerType.api),
    ],
)
def test_test_credential_endpoint_wires_user_agent_header(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    expected: WorkflowRunTriggerType,
) -> None:
    setup_mock = _setup_mock()
    mock_credentials = SimpleNamespace(
        get_credential=AsyncMock(
            return_value=SimpleNamespace(
                credential_type=CredentialType.PASSWORD,
                browser_profile_id=None,
                totp_identifier=None,
                name="Test Credential",
                proxy_location=None,
                proxy_session_id=None,
            )
        )
    )
    monkeypatch.setattr(credentials.app, "DATABASE", SimpleNamespace(credentials=mock_credentials))
    monkeypatch.setattr(credentials.app, "WORKFLOW_SERVICE", _workflow_service(setup_mock))
    _patch_cloud_proxy_session_headers(monkeypatch)
    _patch_executor(monkeypatch)

    app = FastAPI()
    app.add_api_route("/credentials/{credential_id}/test", credentials.test_credential, methods=["POST"])
    app.dependency_overrides[org_auth_service.get_current_org] = _auth_override

    resp = TestClient(app).post(
        "/credentials/cred_test/test",
        json={"url": "https://example.com/login", "save_browser_profile": False},
        headers=headers,
    )

    assert resp.status_code == 200
    setup_mock.assert_awaited_once()
    assert setup_mock.call_args.kwargs["trigger_type"] == expected


@pytest.mark.parametrize(
    "headers, expected",
    [
        ({"x-user-agent": "skyvern-ui"}, WorkflowRunTriggerType.manual),
        ({}, WorkflowRunTriggerType.api),
    ],
)
def test_test_login_endpoint_wires_user_agent_header(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    expected: WorkflowRunTriggerType,
) -> None:
    setup_mock = _setup_mock()
    vault_service = SimpleNamespace(
        create_credential=AsyncMock(
            return_value=SimpleNamespace(vault_type=None, credential_id="cred_test", totp_identifier=None)
        )
    )
    monkeypatch.setattr(credentials, "_get_credential_vault_service", AsyncMock(return_value=vault_service))
    monkeypatch.setattr(credentials, "_create_browser_profile_after_workflow", AsyncMock())
    monkeypatch.setattr(credentials.app, "WORKFLOW_SERVICE", _workflow_service(setup_mock))
    _patch_executor(monkeypatch)

    app = FastAPI()
    app.add_api_route("/credentials/test-login", credentials.test_login, methods=["POST"])
    app.dependency_overrides[org_auth_service.get_current_org] = _auth_override

    resp = TestClient(app).post(
        "/credentials/test-login",
        json={"url": "https://example.com/login", "username": "user@example.com", "password": "pw"},
        headers=headers,
    )

    assert resp.status_code == 200
    setup_mock.assert_awaited_once()
    assert setup_mock.call_args.kwargs["trigger_type"] == expected

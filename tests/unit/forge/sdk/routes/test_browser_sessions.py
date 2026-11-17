import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.responses import ORJSONResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.exceptions import (
    BrowserSessionExtensionUnconfirmed,
    BrowserSessionNotExtendable,
    BrowserSessionStartupTimeout,
    ExternalBrowserUnavailable,
)
from skyvern.forge import app as forge_app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import PersistentBrowserSessionModel
from skyvern.forge.sdk.routes import browser_sessions as browser_sessions_mod
from skyvern.forge.sdk.schemas.persistent_browser_sessions import API_BROWSER_SESSION_CREATED_BY
from skyvern.forge.sdk.schemas.task_v2 import TaskV2
from skyvern.forge.sdk.schemas.tasks import Task, TaskBase, TaskResponse
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunResponseBase
from skyvern.schemas.browser_session_timeouts import max_lifetime_exceeded_warning
from skyvern.schemas.runs import (
    BlockRunResponse,
    TaskRunRequest,
    TaskRunResponse,
    WorkflowRunRequest,
    WorkflowRunResponse,
)
from skyvern.webeye.default_persistent_sessions_manager import DefaultPersistentSessionsManager
from skyvern.webeye.persistent_sessions_manager import (
    BrowserSessionCreditAdmissionRefusal,
    BrowserSessionExtension,
)
from skyvern.webeye.real_browser_state import RealBrowserState
from skyvern.webeye.schemas import BrowserSessionResponse
from tests.unit._external_cdp_fakes import CDP_URL as EXTERNAL_CDP_URL
from tests.unit._external_cdp_fakes import ORG_A, ORG_B
from tests.unit._external_cdp_fakes import TOKEN as EXTERNAL_TOKEN
from tests.unit._external_cdp_fakes import external_browser_state, external_cdp_stack, register_external


async def _extend(app_mock: MagicMock, additional_minutes: int) -> Any:
    built_response = BrowserSessionResponse(
        browser_session_id="pbs_1",
        organization_id="org_1",
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
    )
    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(
            browser_sessions_mod.BrowserSessionResponse, "from_browser_session", AsyncMock(return_value=built_response)
        ),
        patch.object(browser_sessions_mod, "_redact_browser_session_addresses", AsyncMock()),
    ):
        return await browser_sessions_mod.extend_browser_session(
            browser_sessions_mod.ExtendBrowserSessionRequest(additional_minutes=additional_minutes),
            "pbs_1",
            current_org=SimpleNamespace(organization_id="org_1"),
        )


async def _close(app_mock: MagicMock) -> ORJSONResponse:
    with patch.object(browser_sessions_mod, "app", app_mock):
        return await browser_sessions_mod.close_browser_session(
            "pbs_1",
            current_org=SimpleNamespace(organization_id="org_1"),
        )


@pytest.mark.asyncio
async def test_extend_browser_session_returns_404_without_org_owned_session() -> None:
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=None)
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await _extend(app_mock, 30)

    assert exc_info.value.status_code == 404
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session.assert_awaited_once_with("pbs_1", "org_1")
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_extend_browser_session_reports_a_clamped_grant_as_a_warning() -> None:
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(
        return_value=SimpleNamespace(status="running", browser_vendor=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
        return_value=BrowserSessionExtension(session=MagicMock(), granted_minutes=20)
    )

    clamped = await _extend(app_mock, 90)

    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session.assert_awaited_once_with("pbs_1", "org_1", 90)
    assert clamped.warning == max_lifetime_exceeded_warning(90, 20)

    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
        return_value=BrowserSessionExtension(session=MagicMock(), granted_minutes=90)
    )
    assert (await _extend(app_mock, 90)).warning is None


@pytest.mark.asyncio
async def test_extend_browser_session_maps_refusals_to_conflict() -> None:
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=SimpleNamespace(status="completed"))
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock()

    with pytest.raises(HTTPException) as ended:
        await _extend(app_mock, 30)
    assert ended.value.status_code == 409
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session.assert_not_awaited()

    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(
        return_value=SimpleNamespace(status="running", browser_vendor=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
        side_effect=BrowserSessionNotExtendable("browser session has expired", "pbs_1")
    )
    with pytest.raises(HTTPException) as refused:
        await _extend(app_mock, 30)
    assert refused.value.status_code == 409
    assert "expired" in refused.value.detail


@pytest.mark.asyncio
async def test_an_unconfirmed_extension_is_accepted_not_failed() -> None:
    """The signal landed, so a retry adds again. The SDK retries 409 and every 5xx on its own, so the
    only honest answer is a 2xx that says the effect is not confirmed and points at GET."""
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(
        return_value=SimpleNamespace(status="running", browser_vendor=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
        side_effect=BrowserSessionExtensionUnconfirmed("pbs_1")
    )

    accepted = await _extend(app_mock, 30)

    assert accepted.status_code == 202
    body = json.loads(accepted.body)
    assert "Do not retry" in body["warning"]


@pytest.mark.asyncio
async def test_close_browser_session_returns_404_without_org_owned_session() -> None:
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=None)
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        pytest.raises(HTTPException) as exc_info,
    ):
        await browser_sessions_mod.close_browser_session(
            "pbs_foreign",
            current_org=SimpleNamespace(organization_id="org_requester"),
        )

    assert exc_info.value.status_code == 404
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session.assert_awaited_once_with(
        "pbs_foreign", "org_requester"
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_browser_session_skips_close_for_a_completed_session() -> None:
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=SimpleNamespace(status="completed", completed_at=datetime(2026, 1, 1))
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()

    response = await _close(app_mock)

    assert response.status_code == 200
    assert json.loads(response.body) == {"message": "Browser session closed"}
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "completed", "failed"])
async def test_close_browser_session_closes_any_session_without_completed_at(status: str) -> None:
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=SimpleNamespace(status=status, completed_at=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()

    response = await _close(app_mock)

    assert response.status_code == 200
    assert json.loads(response.body) == {"message": "Browser session closed"}
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_awaited_once_with("org_1", "pbs_1")


@pytest.mark.asyncio
async def test_close_browser_session_propagates_close_failure_for_a_live_session() -> None:
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=SimpleNamespace(status="running", completed_at=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock(side_effect=RuntimeError("close failed"))

    with pytest.raises(RuntimeError, match="close failed"):
        await _close(app_mock)


@pytest.mark.asyncio
@pytest.mark.parametrize("needs_live_view", [True, False])
async def test_create_browser_session_forwards_whether_the_session_will_be_watched(
    needs_live_view: bool,
) -> None:
    """Dropped here, the session is still created and still connects — it only fails later, when
    someone opens the live view. Defaulting it False is what keeps unattended automation routable."""
    app_mock = MagicMock()
    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access = AsyncMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=MagicMock())

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod.BrowserSessionResponse, "from_browser_session", AsyncMock()),
        patch.object(browser_sessions_mod, "_redact_browser_session_addresses", AsyncMock()),
    ):
        await browser_sessions_mod.create_browser_session(
            browser_sessions_mod.CreateBrowserSessionRequest(needs_live_view=needs_live_view),
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.await_args.kwargs["needs_live_view"] is needs_live_view


@pytest.mark.asyncio
async def test_create_browser_session_forwards_the_target_site_and_bot_protection() -> None:
    """Dropped here, the session is still created normally, just never on the infrastructure its target needs."""
    app_mock = MagicMock()
    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access = AsyncMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=MagicMock())

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod.BrowserSessionResponse, "from_browser_session", AsyncMock()),
        patch.object(browser_sessions_mod, "_redact_browser_session_addresses", AsyncMock()),
    ):
        await browser_sessions_mod.create_browser_session(
            browser_sessions_mod.CreateBrowserSessionRequest(url="https://example.com/form", bot_protection="hcaptcha"),
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    forwarded = app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.await_args.kwargs
    assert forwarded["url"] == "https://example.com/form"
    assert forwarded["bot_protection"] == "hcaptcha"


@pytest.mark.asyncio
async def test_create_browser_session_preserves_typed_credit_refusal_http_contract() -> None:
    app_mock = MagicMock()
    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access = AsyncMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(side_effect=BrowserSessionCreditAdmissionRefusal())

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        pytest.raises(HTTPException) as exc_info,
    ):
        await browser_sessions_mod.create_browser_session(
            browser_sessions_mod.CreateBrowserSessionRequest(),
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert exc_info.value.status_code == 402
    assert exc_info.value.detail == "Credits exhausted. Upgrade your plan in Billing."


@pytest.mark.asyncio
async def test_create_browser_session_observes_before_creating_the_session() -> None:
    calls: list[str] = []
    app_mock = MagicMock()

    async def observe(**_: object) -> None:
        calls.append("observe")

    async def create_session(**_: object) -> MagicMock:
        calls.append("create")
        return MagicMock()

    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access = AsyncMock(side_effect=observe)
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(side_effect=create_session)

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod.BrowserSessionResponse, "from_browser_session", AsyncMock()),
        patch.object(browser_sessions_mod, "_redact_browser_session_addresses", AsyncMock()),
    ):
        await browser_sessions_mod.create_browser_session(
            browser_sessions_mod.CreateBrowserSessionRequest(),
            current_org=SimpleNamespace(organization_id="org_1"),
            user_id="user_1",
        )

    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access.assert_awaited_once_with(
        organization_id="org_1",
        feature_names={"standalone_browser_sessions"},
    )
    assert calls == ["observe", "create"]


@pytest.mark.asyncio
async def test_create_browser_session_holds_the_admission_scope_until_the_create_ends() -> None:
    """A deployment's per-organization create slot must stay held while the browser starts and come back on failure."""
    calls: list[str] = []

    @asynccontextmanager
    async def admission(organization_id: str) -> AsyncIterator[None]:
        calls.append(f"admit {organization_id}")
        try:
            yield
        finally:
            calls.append("release")

    async def create_session(**_: object) -> MagicMock:
        calls.append("create")
        raise BrowserSessionStartupTimeout("pbs_1")

    app_mock = MagicMock()
    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access = AsyncMock()
    app_mock.AGENT_FUNCTION.browser_session_create_admission = admission
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = create_session

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod, "_redact_browser_session_addresses", AsyncMock()),
        pytest.raises(BrowserSessionStartupTimeout),
    ):
        await browser_sessions_mod.create_browser_session(
            browser_sessions_mod.CreateBrowserSessionRequest(),
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert calls == ["admit org_1", "create", "release"]


def test_a_session_request_is_unwatched_unless_it_says_otherwise() -> None:
    """The public default. True would route every API-created session to first-party and make the
    provider rollout unreachable."""
    assert browser_sessions_mod.CreateBrowserSessionRequest().needs_live_view is False


@pytest.mark.asyncio
async def test_get_browser_session_enables_strict_download_lookup() -> None:
    browser_session = MagicMock()
    response = MagicMock()
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=browser_session)
    app_mock.AGENT_FUNCTION.browser_session_ids_authorized_for_connect = AsyncMock(return_value=set())
    from_browser_session = AsyncMock(return_value=response)

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod.BrowserSessionResponse, "from_browser_session", from_browser_session),
    ):
        result = await browser_sessions_mod.get_browser_session(
            "pbs_1",
            current_org=SimpleNamespace(organization_id="org_1"),
        )

    assert result is response
    from_browser_session.assert_awaited_once_with(
        browser_session,
        app_mock.STORAGE,
        fail_download_lookup=True,
        include_stream_transport=True,
        concurrent_listings=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["read", "history", "active", "workflow_runs"])
@pytest.mark.parametrize("connect_authorized", [False, True])
async def test_browser_session_read_responses_only_reveal_connect_token_to_authorized_caller(
    endpoint: str, connect_authorized: bool
) -> None:
    session_id = "pbs_test_token_redaction"
    organization_id = "org_test_token_redaction"
    address = "wss://router.test/pbs_test_token_redaction?token=secret&region=eu"
    session = SimpleNamespace(
        persistent_browser_session_id=session_id,
        organization_id=organization_id,
        browser_profile_id=None,
    )
    response = BrowserSessionResponse(
        browser_session_id=session_id,
        organization_id=organization_id,
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
        browser_address=address,
        vnc_streaming_supported=True,
    )
    app_mock = MagicMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=session)
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_active_sessions = AsyncMock(return_value=[session])
    app_mock.DATABASE.browser_sessions.get_persistent_browser_sessions_history = AsyncMock(return_value=[session])
    app_mock.DATABASE.browser_sessions.get_folder_visible_browser_session_ids = AsyncMock(return_value={session_id})
    app_mock.AGENT_FUNCTION.accessible_browser_session_ids = AsyncMock(return_value={session_id})
    app_mock.AGENT_FUNCTION.redact_inaccessible_browser_session_artifacts = AsyncMock(return_value=False)
    app_mock.AGENT_FUNCTION.browser_session_ids_authorized_for_connect = AsyncMock(
        return_value={session_id} if connect_authorized else set()
    )
    app_mock.WORKFLOW_SERVICE.get_workflow_runs_for_browser_session = AsyncMock(
        return_value=[WorkflowRun.model_construct(browser_session_id=session_id, browser_address=address)]
    )
    principal = SimpleNamespace(organization_id=organization_id, api_key_access_mode="member")

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod, "get_request_principal", return_value=principal),
        patch.object(
            browser_sessions_mod.BrowserSessionResponse,
            "from_browser_session",
            AsyncMock(return_value=response),
        ),
    ):
        if endpoint == "read":
            result = await browser_sessions_mod.get_browser_session(
                session_id, current_org=SimpleNamespace(organization_id=organization_id)
            )
            returned_address = result.browser_address
        elif endpoint == "history":
            result = await browser_sessions_mod.get_browser_sessions_all(
                current_org=SimpleNamespace(organization_id=organization_id), page=1, page_size=10
            )
            returned_address = result[0].browser_address
        elif endpoint == "active":
            result = await browser_sessions_mod.get_browser_sessions(
                current_org=SimpleNamespace(organization_id=organization_id)
            )
            returned_address = result[0].browser_address
        else:
            result = await browser_sessions_mod.get_workflow_runs_for_browser_session(
                session_id,
                page=1,
                page_size=10,
                current_org=SimpleNamespace(organization_id=organization_id),
            )
            returned_address = result[0].browser_address

    expected_address = address if connect_authorized else None
    assert returned_address == expected_address
    if endpoint != "workflow_runs":
        session_response = result if endpoint == "read" else result[0]
        assert session_response.vnc_streaming_supported is connect_authorized
        assert session_response.browser_address_redacted is not connect_authorized
    app_mock.AGENT_FUNCTION.browser_session_ids_authorized_for_connect.assert_awaited_once_with(
        principal, organization_id, {session_id}
    )


@pytest.mark.asyncio
async def test_browser_session_address_redaction_covers_nested_run_and_task_responses() -> None:
    session_id = "pbs_test_nested_redaction"
    organization_id = "org_test_nested_redaction"
    address = f"wss://router.test/{session_id}?token=secret"
    browser_session = BrowserSessionResponse(
        browser_session_id=session_id,
        organization_id=organization_id,
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
        browser_address=address,
        vnc_streaming_supported=True,
    )
    run = WorkflowRun.model_construct(browser_session_id=session_id, browser_address=address)
    task = Task.model_construct(browser_session_id=session_id, browser_address=address)
    task_response = TaskResponse.model_construct(
        browser_session_id=session_id, request=TaskBase.model_construct(browser_address=address)
    )
    workflow_response = WorkflowRunResponseBase.model_construct(
        browser_session_id=session_id,
        browser_address=address,
        task_v2=TaskV2.model_construct(browser_address=address),
    )
    task_run_response = TaskRunResponse.model_construct(
        run_request=TaskRunRequest.model_construct(browser_session_id=session_id, browser_address=address)
    )
    workflow_run_response = WorkflowRunResponse.model_construct(
        browser_session_id=session_id,
        run_request=WorkflowRunRequest.model_construct(browser_session_id=session_id, browser_address=address),
    )
    block_run_response = BlockRunResponse.model_construct(
        browser_session_id=session_id,
        run_request=WorkflowRunRequest.model_construct(browser_session_id=session_id, browser_address=address),
    )
    task_v2 = TaskV2.model_construct(browser_address=address)
    unattached_task = Task.model_construct(browser_session_id=None, browser_address=address)
    principal = SimpleNamespace(organization_id=organization_id, api_key_access_mode="member")
    authorization = AsyncMock(return_value=set())
    unresolved = AsyncMock(return_value=False)
    app_mock = SimpleNamespace(
        AGENT_FUNCTION=SimpleNamespace(
            browser_session_ids_authorized_for_connect=authorization,
            should_redact_unresolved_browser_session_address=unresolved,
        )
    )

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod, "get_request_principal", return_value=principal),
    ):
        await browser_sessions_mod._redact_browser_session_addresses(
            [
                browser_session,
                run,
                task,
                task_response,
                workflow_response,
                task_run_response,
                workflow_run_response,
                block_run_response,
                task_v2,
                unattached_task,
            ],
            organization_id,
            task_v2_browser_session_id=session_id,
        )

    assert all(
        response.browser_address is None for response in (browser_session, run, task, workflow_response, task_v2)
    )
    assert task_response.request.browser_address is None
    assert workflow_response.task_v2.browser_address is None
    assert task_run_response.run_request.browser_address is None
    assert workflow_run_response.run_request.browser_address is None
    assert block_run_response.run_request.browser_address is None
    assert browser_session.vnc_streaming_supported is False
    assert browser_session.browser_address_redacted is True
    assert unattached_task.browser_address is None
    unresolved.assert_not_awaited()
    authorization.assert_awaited_once_with(principal, organization_id, {session_id})


@pytest.mark.asyncio
async def test_browser_session_address_redaction_infers_router_ids_in_mixed_responses() -> None:
    authorized_session_id = "pbs_test_authorized_mixed_redaction"
    inferred_session_id = "pbs_test_inferred_mixed_redaction"
    authorized_address = f"wss://router.test/{authorized_session_id}?token=authorized"
    inferred_address = f"wss://router.test/{inferred_session_id}?token=secret"
    authorized_task = Task.model_construct(browser_session_id=authorized_session_id, browser_address=authorized_address)
    task_without_id = Task.model_construct(browser_session_id=None, browser_address=inferred_address)
    external_task = Task.model_construct(
        browser_session_id=None, browser_address="wss://remote.test/devtools/browser/id?token=external"
    )
    nested_task_v2 = TaskV2.model_construct(browser_address=inferred_address)
    workflow_response = WorkflowRunResponseBase.model_construct(task_v2=nested_task_v2)
    organization_id = "org_test_mixed_address_redaction"
    principal = SimpleNamespace(organization_id=organization_id, api_key_access_mode="member")
    authorization = AsyncMock(return_value={authorized_session_id})
    unresolved = AsyncMock(return_value=False)
    app_mock = SimpleNamespace(
        AGENT_FUNCTION=SimpleNamespace(
            browser_session_ids_authorized_for_connect=authorization,
            should_redact_unresolved_browser_session_address=unresolved,
        )
    )

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod, "get_request_principal", return_value=principal),
    ):
        await browser_sessions_mod._redact_browser_session_addresses(
            [authorized_task, task_without_id, external_task, workflow_response], organization_id
        )

    assert authorized_task.browser_address == authorized_address
    assert task_without_id.browser_address is None
    assert external_task.browser_address == "wss://remote.test/devtools/browser/id?token=external"
    assert nested_task_v2.browser_address is None
    authorization.assert_awaited_once_with(principal, organization_id, {authorized_session_id, inferred_session_id})
    unresolved.assert_awaited_once_with(principal, organization_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "session_id"),
    [
        ("wss://router.test/pbs_query_api_key?api_key=pbs_query_api_key.secret", "pbs_query_api_key"),
        (
            "wss://router.test/pbs_query_access_token?access_token=pbs_query_access_token.secret",
            "pbs_query_access_token",
        ),
        (
            "wss://router.test/pbs_query_header_key?x-api-key=pbs_query_header_key.secret",
            "pbs_query_header_key",
        ),
        ("wss://router.test/api_key/pbs_path_key.secret/pbs_path_key", "pbs_path_key"),
        ("wss://router.test/pbs_discovery/json/version?token=secret", "pbs_discovery"),
        (
            "https://router.test/token/pbs_path_token.secret/pbs_discovery_list/json/list",
            "pbs_discovery_list",
        ),
    ],
)
async def test_browser_session_redaction_infers_proxy_query_path_and_discovery_ids(
    address: str, session_id: str
) -> None:
    organization_id = "org_test_router_address_forms"
    task = Task.model_construct(browser_session_id=None, browser_address=address)
    principal = SimpleNamespace(organization_id=organization_id, api_key_access_mode="member")
    authorization = AsyncMock(return_value={session_id})
    unresolved = AsyncMock(return_value=True)
    app_mock = SimpleNamespace(
        AGENT_FUNCTION=SimpleNamespace(
            browser_session_ids_authorized_for_connect=authorization,
            should_redact_unresolved_browser_session_address=unresolved,
        )
    )

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod, "get_request_principal", return_value=principal),
    ):
        await browser_sessions_mod._redact_browser_session_addresses([task], organization_id)

    assert task.browser_address == address
    authorization.assert_awaited_once_with(principal, organization_id, {session_id})
    unresolved.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_session_redaction_fails_closed_for_unresolved_router_credentials() -> None:
    organization_id = "org_test_unresolved_router_address"
    address = "wss://router.test/token/bearer-without-session-id"
    task = Task.model_construct(browser_session_id=None, browser_address=address)
    principal = SimpleNamespace(organization_id=organization_id, api_key_access_mode="scoped")
    authorization = AsyncMock()
    unresolved = AsyncMock(return_value=True)
    app_mock = SimpleNamespace(
        AGENT_FUNCTION=SimpleNamespace(
            browser_session_ids_authorized_for_connect=authorization,
            should_redact_unresolved_browser_session_address=unresolved,
        )
    )

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod, "get_request_principal", return_value=principal),
    ):
        await browser_sessions_mod._redact_browser_session_addresses([task], organization_id)

    assert task.browser_address is None
    authorization.assert_not_awaited()
    unresolved.assert_awaited_once_with(principal, organization_id)


@pytest.mark.asyncio
async def test_browser_session_redaction_fails_closed_for_unrecognized_connect_address() -> None:
    organization_id = "org_test_unrecognized_connect_address"
    address = "wss://router.test/devtools/browser/browser-id"
    task = Task.model_construct(browser_session_id=None, browser_address=address)
    principal = SimpleNamespace(organization_id=organization_id, api_key_access_mode="member")
    authorization = AsyncMock()
    unresolved = AsyncMock(return_value=True)
    app_mock = SimpleNamespace(
        AGENT_FUNCTION=SimpleNamespace(
            browser_session_ids_authorized_for_connect=authorization,
            should_redact_unresolved_browser_session_address=unresolved,
        )
    )

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod, "get_request_principal", return_value=principal),
    ):
        await browser_sessions_mod._redact_browser_session_addresses([task], organization_id)

    assert task.browser_address is None
    authorization.assert_not_awaited()
    unresolved.assert_awaited_once_with(principal, organization_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_session_id_matches", [True, False])
async def test_browser_session_redaction_binds_legacy_router_address_to_session(
    declared_session_id_matches: bool,
) -> None:
    organization_id = "org_test_legacy_router_address"
    session_id = "pbs_test_legacy_router_address"
    declared_session_id = session_id if declared_session_id_matches else "pbs_test_different_session"
    address = f"wss://sessions.test/{session_id}/tok.sig/devtools/browser/browser-id"
    task = Task.model_construct(browser_session_id=declared_session_id, browser_address=address)
    principal = SimpleNamespace(organization_id=organization_id, api_key_access_mode="member")
    authorization = AsyncMock(return_value={session_id, declared_session_id})
    unresolved = AsyncMock(return_value=True)
    app_mock = SimpleNamespace(
        AGENT_FUNCTION=SimpleNamespace(
            browser_session_ids_authorized_for_connect=authorization,
            should_redact_unresolved_browser_session_address=unresolved,
        )
    )

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod, "get_request_principal", return_value=principal),
    ):
        await browser_sessions_mod._redact_browser_session_addresses([task], organization_id)

    assert task.browser_address == (address if declared_session_id_matches else None)
    authorization.assert_awaited_once_with(principal, organization_id, {session_id, declared_session_id})
    if declared_session_id_matches:
        unresolved.assert_not_awaited()
    else:
        unresolved.assert_awaited_once_with(principal, organization_id)


@pytest.mark.asyncio
async def test_browser_session_redaction_authorizes_both_ids_for_ambiguous_router_address() -> None:
    organization_id = "org_test_ambiguous_router_address"
    allowed_session_id = "pbs_allowed"
    restricted_session_id = "pbs_restricted"
    address = f"wss://router.test/{allowed_session_id}/unused/devtools/browser/{restricted_session_id}?token=secret"
    task = Task.model_construct(browser_session_id=None, browser_address=address)
    principal = SimpleNamespace(organization_id=organization_id, api_key_access_mode="member")
    authorization = AsyncMock(return_value={allowed_session_id})
    unresolved = AsyncMock(return_value=True)
    app_mock = SimpleNamespace(
        AGENT_FUNCTION=SimpleNamespace(
            browser_session_ids_authorized_for_connect=authorization,
            should_redact_unresolved_browser_session_address=unresolved,
        )
    )

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod, "get_request_principal", return_value=principal),
    ):
        await browser_sessions_mod._redact_browser_session_addresses([task], organization_id)

    assert task.browser_address is None
    authorization.assert_awaited_once_with(principal, organization_id, {allowed_session_id, restricted_session_id})
    unresolved.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_session_redaction_fails_closed_for_unresolved_address_in_mixed_batch() -> None:
    organization_id = "org_test_mixed_unresolved_address"
    session_id = "pbs_test_authorized_address"
    authorized = Task.model_construct(
        browser_session_id=None,
        browser_address=f"wss://router.test/{session_id}?token=authorized-secret",
    )
    unresolved = Task.model_construct(
        browser_session_id=None,
        browser_address="wss://router.test/token/opaque-secret",
    )
    unresolved_with_session_id = Task.model_construct(
        browser_session_id=session_id,
        browser_address="wss://router.test/token/opaque-secret",
    )
    principal = SimpleNamespace(organization_id=organization_id, api_key_access_mode="member")
    authorization = AsyncMock(return_value={session_id})
    unresolved_policy = AsyncMock(return_value=True)
    app_mock = SimpleNamespace(
        AGENT_FUNCTION=SimpleNamespace(
            browser_session_ids_authorized_for_connect=authorization,
            should_redact_unresolved_browser_session_address=unresolved_policy,
        )
    )

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod, "get_request_principal", return_value=principal),
    ):
        await browser_sessions_mod._redact_browser_session_addresses(
            [authorized, unresolved, unresolved_with_session_id], organization_id
        )

    assert authorized.browser_address.endswith("authorized-secret")
    assert unresolved.browser_address is None
    assert unresolved_with_session_id.browser_address is None
    authorization.assert_awaited_once_with(principal, organization_id, {session_id})
    unresolved_policy.assert_awaited_once_with(principal, organization_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("connect_authorized", [False, True])
async def test_vnc_only_browser_session_still_requires_connect_authorization(connect_authorized: bool) -> None:
    organization_id = "org_test_vnc_only_redaction"
    session_id = "pbs_test_vnc_only_redaction"
    response = BrowserSessionResponse(
        browser_session_id=session_id,
        organization_id=organization_id,
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
        browser_address=None,
        vnc_streaming_supported=True,
    )
    principal = SimpleNamespace(organization_id=organization_id, api_key_access_mode="member")
    authorization = AsyncMock(return_value={session_id} if connect_authorized else set())
    app_mock = SimpleNamespace(AGENT_FUNCTION=SimpleNamespace(browser_session_ids_authorized_for_connect=authorization))

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod, "get_request_principal", return_value=principal),
    ):
        await browser_sessions_mod._redact_browser_session_addresses([response], organization_id)

    assert response.browser_address is None
    assert response.vnc_streaming_supported is connect_authorized
    assert response.browser_address_redacted is not connect_authorized
    authorization.assert_awaited_once_with(principal, organization_id, {session_id})


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["create", "extend", "extend_unconfirmed", "update"])
@pytest.mark.parametrize("connect_authorized", [False, True])
async def test_browser_session_mutation_responses_only_reveal_connect_token_to_authorized_caller(
    endpoint: str, connect_authorized: bool
) -> None:
    session_id = "pbs_test_mutation_token_redaction"
    organization_id = "org_test_mutation_token_redaction"
    address = f"wss://router.test/{session_id}?token=secret&region=eu"
    session = SimpleNamespace(status="running", browser_vendor=None)
    response = BrowserSessionResponse(
        browser_session_id=session_id,
        organization_id=organization_id,
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
        browser_address=address,
        vnc_streaming_supported=True,
    )
    app_mock = MagicMock()
    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access = AsyncMock()
    app_mock.AGENT_FUNCTION.browser_session_ids_authorized_for_connect = AsyncMock(
        return_value={session_id} if connect_authorized else set()
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=session)
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=session)
    app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
        return_value=BrowserSessionExtension(session=session, granted_minutes=30)
    )
    app_mock.DATABASE.browser_sessions.update_persistent_browser_session = AsyncMock(return_value=session)
    principal = SimpleNamespace(organization_id=organization_id, api_key_access_mode="member")

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod, "get_request_principal", return_value=principal),
        patch.object(
            browser_sessions_mod.BrowserSessionResponse,
            "from_browser_session",
            AsyncMock(return_value=response),
        ),
    ):
        if endpoint == "create":
            result = await browser_sessions_mod.create_browser_session(
                browser_sessions_mod.CreateBrowserSessionRequest(),
                current_org=SimpleNamespace(organization_id=organization_id),
            )
        elif endpoint in {"extend", "extend_unconfirmed"}:
            if endpoint == "extend_unconfirmed":
                app_mock.PERSISTENT_SESSIONS_MANAGER.extend_session = AsyncMock(
                    side_effect=BrowserSessionExtensionUnconfirmed(session_id)
                )
            result = await browser_sessions_mod.extend_browser_session(
                browser_sessions_mod.ExtendBrowserSessionRequest(additional_minutes=30),
                session_id,
                current_org=SimpleNamespace(organization_id=organization_id),
            )
        else:
            result = await browser_sessions_mod.update_browser_session(
                browser_sessions_mod.UpdateBrowserSessionRequest(generate_browser_profile=True),
                session_id,
                current_org=SimpleNamespace(organization_id=organization_id),
            )

    returned_address = (
        json.loads(result.body)["browser_address"] if isinstance(result, ORJSONResponse) else result.browser_address
    )
    expected_address = address if connect_authorized else None
    assert returned_address == expected_address
    returned_vnc_support = (
        json.loads(result.body)["vnc_streaming_supported"]
        if isinstance(result, ORJSONResponse)
        else result.vnc_streaming_supported
    )
    assert returned_vnc_support is connect_authorized
    returned_redacted = (
        json.loads(result.body)["browser_address_redacted"]
        if isinstance(result, ORJSONResponse)
        else result.browser_address_redacted
    )
    assert returned_redacted is not connect_authorized
    app_mock.AGENT_FUNCTION.browser_session_ids_authorized_for_connect.assert_awaited_once_with(
        principal, organization_id, {session_id}
    )


@pytest.mark.parametrize("endpoint", ["history", "active"])
@pytest.mark.asyncio
async def test_fan_out_listings_keep_each_sessions_storage_listings_sequential(endpoint: str) -> None:
    """These endpoints already gather across sessions, so overlapping a session's two listings would
    only raise the request's peak pool checkouts from N to 2N."""
    sessions = [MagicMock(browser_profile_id=None, persistent_browser_session_id=f"pbs_{index}") for index in range(2)]
    app_mock = MagicMock()
    app_mock.AGENT_FUNCTION.enforce_browser_session_credential_use = AsyncMock()
    app_mock.AGENT_FUNCTION.redact_inaccessible_browser_session_artifacts = AsyncMock(return_value=False)
    app_mock.AGENT_FUNCTION.browser_session_ids_authorized_for_connect = AsyncMock(return_value=set())
    app_mock.AGENT_FUNCTION.accessible_browser_session_ids = AsyncMock(
        return_value={session.persistent_browser_session_id for session in sessions}
    )
    app_mock.DATABASE.browser_sessions.get_persistent_browser_sessions_history = AsyncMock(return_value=sessions)
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_active_sessions = AsyncMock(return_value=sessions)
    app_mock.DATABASE.browser_sessions.get_folder_visible_browser_session_ids = AsyncMock(
        return_value={session.persistent_browser_session_id for session in sessions}
    )
    from_browser_session = AsyncMock(return_value=MagicMock())

    with (
        patch.object(browser_sessions_mod, "app", app_mock),
        patch.object(browser_sessions_mod.BrowserSessionResponse, "from_browser_session", from_browser_session),
    ):
        if endpoint == "history":
            await browser_sessions_mod.get_browser_sessions_all(
                current_org=SimpleNamespace(organization_id="org_1"), page=1, page_size=100
            )
        else:
            await browser_sessions_mod.get_browser_sessions(current_org=SimpleNamespace(organization_id="org_1"))

    assert from_browser_session.await_count == len(sessions)
    for call in from_browser_session.await_args_list:
        assert call.kwargs.get("concurrent_listings", False) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "expected_stored_created_by", "expected_response_created_by"),
    [
        ("user_creator", "user_creator", "user_creator"),
        (None, API_BROWSER_SESSION_CREATED_BY, None),
    ],
)
async def test_create_and_history_hide_api_origin_but_preserve_real_user_id(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_engine: AsyncEngine,
    user_id: str | None,
    expected_stored_created_by: str,
    expected_response_created_by: str | None,
) -> None:
    db = AgentDB("sqlite+aiosqlite:///:memory:", db_engine=sqlite_engine)
    org = await db.organizations.create_organization(organization_name="Creator Org")
    monkeypatch.setattr(forge_app.DATABASE, "browser_sessions", db.browser_sessions)
    monkeypatch.setattr(forge_app, "PERSISTENT_SESSIONS_MANAGER", DefaultPersistentSessionsManager(database=db))
    monkeypatch.setattr(forge_app, "AGENT_FUNCTION", AgentFunction())
    monkeypatch.setattr(forge_app, "STORAGE", None)

    created = await browser_sessions_mod.create_browser_session(
        browser_sessions_mod.CreateBrowserSessionRequest(),
        current_org=org,
        user_id=user_id,
    )
    [listed] = await browser_sessions_mod.get_browser_sessions_all(current_org=org, page=1, page_size=10)
    [stored] = await db.browser_sessions.get_persistent_browser_sessions_history(
        org.organization_id, page=1, page_size=10
    )

    assert created.model_dump()["created_by"] == expected_response_created_by
    assert listed.model_dump()["created_by"] == expected_response_created_by
    assert stored.created_by == expected_stored_created_by


@pytest_asyncio.fixture
async def external_stack() -> AsyncIterator[SimpleNamespace]:
    async with external_cdp_stack() as built:
        yield built


async def _external_row_count(stack: SimpleNamespace) -> int:
    async with stack.session_factory() as session:
        return len((await session.execute(PersistentBrowserSessionModel.__table__.select())).all())


@pytest.mark.asyncio
async def test_external_registration_refuses_a_private_host_outside_local_development(
    external_stack: SimpleNamespace,
) -> None:
    probe = AsyncMock()
    with (
        patch.object(settings, "ENV", "production"),
        patch.object(browser_sessions_mod, "connect_external_cdp_browser", probe),
    ):
        for address in ("ws://10.0.0.5:9222/devtools/browser/b1", "http://169.254.169.254:9222"):
            response = await external_stack.client.post("/v1/browser_sessions/external", json={"cdp_url": address})
            assert response.status_code == 400
        plaintext = await external_stack.client.post(
            "/v1/browser_sessions/external", json={"cdp_url": "ws://93.184.216.34:9222/devtools/browser/b1"}
        )
        assert plaintext.status_code == 422 and "wss://" in plaintext.text
    probe.assert_not_awaited()
    assert await _external_row_count(external_stack) == 0


@pytest.mark.asyncio
async def test_an_invalid_external_registration_never_echoes_the_submitted_address(
    external_stack: SimpleNamespace,
) -> None:
    with capture_logs() as logs:
        for body in ({"cdp_url": f"not a url {EXTERNAL_TOKEN}"}, {"cdp_url": EXTERNAL_CDP_URL, "timeout": 100_000}):
            response = await external_stack.client.post("/v1/browser_sessions/external", json=body)
            assert response.status_code == 422
            assert EXTERNAL_TOKEN not in response.text
    assert EXTERNAL_TOKEN not in repr(logs)


@pytest.mark.asyncio
async def test_a_registration_is_listed_to_its_org_as_external_with_its_remaining_lifetime(
    external_stack: SimpleNamespace,
) -> None:
    with capture_logs() as logs:
        session_id = (await register_external(external_stack.client, timeout=30)).json()["browser_session_id"]
        fetched = await external_stack.client.get(f"/v1/browser_sessions/{session_id}")
        listed = await external_stack.client.get("/v1/browser_sessions")
        history = await external_stack.client.get("/v1/browser_sessions/history")
        external_stack.caller.organization_id = ORG_B
        foreign_get = await external_stack.client.get(f"/v1/browser_sessions/{session_id}")
        foreign_list = await external_stack.client.get("/v1/browser_sessions")
        foreign_history = await external_stack.client.get("/v1/browser_sessions/history")
    external_stack.caller.organization_id = ORG_A

    listed_rows = [row for row in listed.json() if row["browser_session_id"] == session_id]
    history_rows = [row for row in history.json() if row["browser_session_id"] == session_id]
    assert len(listed_rows) == len(history_rows) == 1
    for body in (fetched.json(), listed_rows[0], history_rows[0]):
        assert body["source"] == "external"
        assert body["status"] == "running"
        assert body["browser_address"] is None
        assert 29 * 60 < body["remaining_lifetime_seconds"] <= 30 * 60
    assert foreign_get.status_code == 404
    assert foreign_list.json() == [] and foreign_history.json() == []
    for response in (fetched, listed, history, foreign_get):
        for leaked in (EXTERNAL_TOKEN, "devtools/browser/b1", ":9333"):
            assert leaked not in response.text
    assert EXTERNAL_TOKEN not in repr(logs)


async def _reject_upgrade(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.read(4096)
    writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    await writer.drain()
    writer.close()


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["refused", "rejected"])
async def test_an_unreachable_external_endpoint_reports_its_cause_and_leaves_no_session(
    endpoint: str, external_stack: SimpleNamespace
) -> None:
    server = await asyncio.start_server(_reject_upgrade, "127.0.0.1", 0) if endpoint == "rejected" else None
    port = server.sockets[0].getsockname()[1] if server else _unused_port()
    dead_address = f"ws://127.0.0.1:{port}/devtools/browser/b1?token={EXTERNAL_TOKEN}"
    try:
        with capture_logs() as logs:
            response = await external_stack.client.post("/v1/browser_sessions/external", json={"cdp_url": dead_address})
    finally:
        if server:
            server.close()

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "unavailable" in detail and "connection failed" in detail
    for leaked in (EXTERNAL_TOKEN, "devtools/browser/b1"):
        assert leaked not in response.text
        assert leaked not in repr(logs)
    assert await external_stack.repo.get_uncompleted_persistent_browser_sessions() == []
    async with external_stack.session_factory() as session:
        attempts = (await session.execute(select(PersistentBrowserSessionModel))).scalars().all()
    assert [(row.status, row.upstream_cdp_url) for row in attempts] == [("failed", None)]


@pytest.mark.asyncio
async def test_failed_registration_attempts_are_limited_per_minute(external_stack: SimpleNamespace) -> None:
    probe = AsyncMock(side_effect=ExternalBrowserUnavailable("connection failed"))
    limit = browser_sessions_mod.MAX_EXTERNAL_REGISTRATION_ATTEMPTS_PER_MINUTE
    with patch.object(browser_sessions_mod, "connect_external_cdp_browser", probe):
        statuses = [
            (
                await external_stack.client.post("/v1/browser_sessions/external", json={"cdp_url": EXTERNAL_CDP_URL})
            ).status_code
            for _ in range(limit + 1)
        ]

    assert statuses == [503] * limit + [429]
    assert probe.await_count == limit


@pytest.mark.asyncio
async def test_reservations_abandoned_mid_probe_do_not_lock_the_org_out(external_stack: SimpleNamespace) -> None:
    for _ in range(browser_sessions_mod.MAX_OPEN_EXTERNAL_BROWSER_SESSIONS):
        assert await external_stack.repo.reserve_external_cdp_session(
            "org_a", 60, max_open=100, max_attempts_per_minute=100
        )
    async with external_stack.session_factory() as session:
        await session.execute(update(PersistentBrowserSessionModel).values(created_at=datetime(2000, 1, 1)))
        await session.commit()

    assert (await register_external(external_stack.client)).status_code == 200
    open_rows = await external_stack.repo.get_uncompleted_persistent_browser_sessions()
    assert len(open_rows) == 1 and open_rows[0].upstream_cdp_url is not None


@pytest.mark.asyncio
async def test_a_probe_that_cannot_detach_fails_the_registration(external_stack: SimpleNamespace) -> None:
    probe_state, _, _ = external_browser_state()
    with (
        patch.object(probe_state, "close", AsyncMock(side_effect=TimeoutError())),
        patch.object(browser_sessions_mod, "connect_external_cdp_browser", AsyncMock(return_value=probe_state)),
    ):
        response = await external_stack.client.post("/v1/browser_sessions/external", json={"cdp_url": EXTERNAL_CDP_URL})

    assert response.status_code == 503 and "connection failed" in response.json()["detail"]
    assert await external_stack.repo.get_uncompleted_persistent_browser_sessions() == []


@pytest.mark.asyncio
async def test_registration_probes_beyond_the_process_bound_are_refused_at_once(
    external_stack: SimpleNamespace,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    probe_state, _, _ = external_browser_state()

    async def slow_probe(_: str) -> RealBrowserState:
        started.set()
        await release.wait()
        return probe_state

    body = {"cdp_url": EXTERNAL_CDP_URL}
    with (
        patch.object(browser_sessions_mod, "_REGISTRATION_PROBES", asyncio.Semaphore(1)),
        patch.object(browser_sessions_mod, "connect_external_cdp_browser", slow_probe),
    ):
        first = asyncio.create_task(external_stack.client.post("/v1/browser_sessions/external", json=body))
        await started.wait()
        try:
            busy = await asyncio.wait_for(
                external_stack.client.post("/v1/browser_sessions/external", json=body), timeout=5
            )
        finally:
            release.set()
        assert (await first).status_code == 200

    assert busy.status_code == 429
    assert len(await external_stack.repo.get_uncompleted_persistent_browser_sessions()) == 1


@pytest.mark.asyncio
async def test_an_external_registration_cannot_be_extended(external_stack: SimpleNamespace) -> None:
    session_id = (await register_external(external_stack.client)).json()["browser_session_id"]
    response = await external_stack.client.post(
        f"/v1/browser_sessions/{session_id}/extend", json={"additional_minutes": 5}
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_external_registrations_are_capped_per_organization(external_stack: SimpleNamespace) -> None:
    for _ in range(browser_sessions_mod.MAX_OPEN_EXTERNAL_BROWSER_SESSIONS):
        assert (await register_external(external_stack.client)).status_code == 200

    refused = await register_external(external_stack.client)

    assert refused.status_code == 429
    assert await _external_row_count(external_stack) == browser_sessions_mod.MAX_OPEN_EXTERNAL_BROWSER_SESSIONS

    async with external_stack.session_factory() as session:
        await session.execute(update(PersistentBrowserSessionModel).values(started_at=datetime(2000, 1, 1)))
        await session.commit()
    assert (await register_external(external_stack.client)).status_code == 200
    open_rows = await external_stack.repo.get_uncompleted_persistent_browser_sessions()
    assert len(open_rows) == 1 and open_rows[0].upstream_cdp_url is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("browser_status,run_status", [("running", "running"), ("retry", "paused")])
async def test_metadata_reads_orphaned_session_without_any_lifecycle_or_transport_calls(browser_status, run_status):
    session = SimpleNamespace(
        persistent_browser_session_id="pbs_1",
        organization_id="org_1",
        status=browser_status,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=None,
        runnable_id="wr_1",
        timeout_minutes=30,
        browser_address="PRIVATE_TRANSPORT",
        browser_settings={"secret": "PRIVATE_SETTING"},
    )
    run = {
        "workflow_run_id": "wr_1",
        "organization_id": "org_1",
        "browser_session_id": "pbs_1",
        "association_browser_session_id": "pbs_1",
        "workflow_permanent_id": "wpid_1",
        "copilot_session_id": "wcc_1",
        "status": run_status,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "finished_at": None,
        "credits_used": 3,
        "cached_credits_used": 1,
        "prompt": "PRIVATE_PROMPT",
        "parameters": {"password": "PRIVATE_PASSWORD"},
    }

    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("Metadata must not access lifecycle, transport, provider or write functions")

    class BrowserRows:
        async def get_persistent_browser_session(self, session_id, organization_id):
            assert (session_id, organization_id) == ("pbs_1", "org_1")
            return session

        def __getattr__(self, name):
            raise AssertionError("Browser metadata cannot write")

    class WorkflowRows:
        async def get_workflow_metadata_for_browser_session(self, session_id, organization_id):
            assert (session_id, organization_id) == ("pbs_1", "org_1")
            return [run]

        def __getattr__(self, name):
            raise AssertionError("Workflow metadata cannot write")

    app_readonly = SimpleNamespace(
        DATABASE=SimpleNamespace(browser_sessions=BrowserRows(), workflow_runs=WorkflowRows()),
        PERSISTENT_SESSIONS_MANAGER=Forbidden(),
        STORAGE=Forbidden(),
        WORKFLOW_SERVICE=Forbidden(),
        AGENT_FUNCTION=Forbidden(),
    )
    with patch.object(browser_sessions_mod, "app", app_readonly):
        response = await browser_sessions_mod.get_browser_session_metadata(
            "pbs_1", current_org=SimpleNamespace(organization_id="org_1")
        )
    assert response.status == browser_status and response.completed_at is None
    assert response.associated_workflow_runs[0].status == run_status
    assert response.associated_workflow_runs[0].copilot_session_id == "wcc_1"
    assert response.association_index_complete is True
    assert "PRIVATE_" not in response.model_dump_json()
    assert session.status == browser_status and session.timeout_minutes == 30 and session.completed_at is None


@pytest.mark.asyncio
async def test_metadata_wrong_org_or_missing_session_returns_404_before_associations():
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=None)
    app_mock.DATABASE.workflow_runs.get_workflow_metadata_for_browser_session = AsyncMock()
    with patch.object(browser_sessions_mod, "app", app_mock), pytest.raises(HTTPException) as error:
        await browser_sessions_mod.get_browser_session_metadata(
            "pbs_1", current_org=SimpleNamespace(organization_id="org_2")
        )
    assert error.value.status_code == 404
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session.assert_awaited_once_with("pbs_1", "org_2")
    app_mock.DATABASE.workflow_runs.get_workflow_metadata_for_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_truncation_is_reported_without_pagination_or_reconciliation():
    session = SimpleNamespace(
        persistent_browser_session_id="pbs_1",
        organization_id="org_1",
        status="completed",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 1, 2, tzinfo=UTC),
        runnable_id=None,
    )
    run = {
        "workflow_run_id": "wr_1",
        "organization_id": "org_1",
        "browser_session_id": "pbs_1",
        "association_browser_session_id": "pbs_1",
        "workflow_permanent_id": "wpid_1",
        "copilot_session_id": None,
        "status": "completed",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "finished_at": datetime(2026, 1, 2, tzinfo=UTC),
        "credits_used": None,
        "cached_credits_used": None,
    }
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=session)
    app_mock.DATABASE.workflow_runs.get_workflow_metadata_for_browser_session = AsyncMock(return_value=[run] * 101)
    with patch.object(browser_sessions_mod, "app", app_mock):
        response = await browser_sessions_mod.get_browser_session_metadata(
            "pbs_1", current_org=SimpleNamespace(organization_id="org_1")
        )
    assert len(response.associated_workflow_runs) == 100 and response.association_index_complete is False
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session.assert_not_called()


@pytest.mark.asyncio
async def test_metadata_missing_stored_status_fails_without_association_or_lifecycle_access():
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=SimpleNamespace(status=None)
    )
    app_mock.DATABASE.workflow_runs.get_workflow_metadata_for_browser_session = AsyncMock()
    with patch.object(browser_sessions_mod, "app", app_mock), pytest.raises(HTTPException) as error:
        await browser_sessions_mod.get_browser_session_metadata(
            "pbs_1", current_org=SimpleNamespace(organization_id="org_1")
        )
    assert error.value.status_code == 503 and error.value.detail == {"code": "browser_session_metadata_unavailable"}
    app_mock.DATABASE.workflow_runs.get_workflow_metadata_for_browser_session.assert_not_awaited()
    app_mock.PERSISTENT_SESSIONS_MANAGER.get_session.assert_not_called()

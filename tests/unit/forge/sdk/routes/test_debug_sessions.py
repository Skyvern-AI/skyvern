from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge.sdk.copilot.active_run_session import ActiveRunSessionAssociation
from skyvern.forge.sdk.routes import debug_sessions as debug_sessions_mod
from skyvern.forge.sdk.schemas.debug_sessions import DebugSession
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import debug_session_browser as debug_session_browser_mod
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.services.request_principal import AuthKind, RequestPrincipal
from skyvern.schemas.browser_session_kind import BrowserSessionKind
from skyvern.schemas.runs import ProxyLocation
from skyvern.webeye.persistent_session_errors import BrowserSessionCreditAdmissionRefusal


@contextmanager
def _patched_app(app_mock: MagicMock) -> Iterator[None]:
    with patch.object(debug_sessions_mod, "app", app_mock), patch.object(debug_session_browser_mod, "app", app_mock):
        yield


@pytest.mark.asyncio
async def test_prewarm_debug_session_dispatches_an_unattached_live_browser() -> None:
    browser_session = SimpleNamespace(
        persistent_browser_session_id="pbs_prewarm",
        ip_address=None,
        browser_address=None,
        browser_profile_id=None,
    )
    app_mock = MagicMock()
    app_mock.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=True)
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(return_value=None)
    app_mock.DATABASE.browser_sessions.mark_prewarm_dispatched = AsyncMock(return_value=True)
    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access = AsyncMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=browser_session)

    with _patched_app(app_mock):
        result = await debug_sessions_mod.prewarm_debug_session(
            request=SimpleNamespace(proxy_location=None),
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result.status_code == 202
    assert result.body == b""
    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access.assert_not_awaited()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.assert_awaited_once_with(
        organization_id="org_123",
        timeout_minutes=debug_sessions_mod.settings.DEBUG_SESSION_TIMEOUT_MINUTES,
        proxy_location=debug_sessions_mod.runtime_proxy_location(None),
        bound_workflow_permanent_id=debug_sessions_mod.PREWARM_BOUND_WORKFLOW_PERMANENT_ID,
        bound_key=debug_sessions_mod._prewarm_bound_key("org_123", "user_123"),
        runnable_type=debug_sessions_mod.PREWARM_PENDING_RUNNABLE_TYPE,
        wait_for_startup=False,
        needs_live_view=True,
        created_by="user_123",
        session_kind=BrowserSessionKind.editor_prewarm,
    )
    app_mock.DATABASE.browser_sessions.mark_prewarm_dispatched.assert_awaited_once_with(
        session_id="pbs_prewarm",
        organization_id="org_123",
        expected_bound_workflow_permanent_id=debug_sessions_mod.PREWARM_BOUND_WORKFLOW_PERMANENT_ID,
        expected_bound_key=debug_sessions_mod._prewarm_bound_key("org_123", "user_123"),
        expected_runnable_type=debug_sessions_mod.PREWARM_PENDING_RUNNABLE_TYPE,
        dispatched_runnable_type=debug_sessions_mod.PREWARM_DISPATCHED_RUNNABLE_TYPE,
    )
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session.assert_awaited_once_with(
        organization_id="org_123",
        workflow_permanent_id=debug_sessions_mod.PREWARM_BOUND_WORKFLOW_PERMANENT_ID,
        bound_key=debug_sessions_mod._prewarm_bound_key("org_123", "user_123"),
    )


@pytest.mark.asyncio
async def test_prewarm_debug_session_reuses_the_live_user_binding() -> None:
    browser_session = SimpleNamespace(persistent_browser_session_id="pbs_prewarm")
    app_mock = MagicMock()
    app_mock.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=True)
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(
        return_value=browser_session
    )

    with _patched_app(app_mock):
        result = await debug_sessions_mod.prewarm_debug_session(
            request=SimpleNamespace(proxy_location=None),
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result.status_code == 202
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.assert_not_called()


@pytest.mark.asyncio
async def test_prewarm_debug_session_is_a_noop_when_the_rollout_is_disabled() -> None:
    app_mock = MagicMock()
    app_mock.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)

    with _patched_app(app_mock):
        result = await debug_sessions_mod.prewarm_debug_session(
            request=SimpleNamespace(proxy_location=None),
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result.status_code == 202
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session.assert_not_called()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.assert_not_called()


@pytest.mark.asyncio
async def test_prewarm_debug_session_treats_a_concurrent_binding_as_success() -> None:
    browser_session = SimpleNamespace(persistent_browser_session_id="pbs_winner")
    app_mock = MagicMock()
    app_mock.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=True)
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(
        side_effect=[None, browser_session]
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(
        side_effect=IntegrityError("INSERT", {}, Exception("duplicate live binding"))
    )

    with _patched_app(app_mock):
        result = await debug_sessions_mod.prewarm_debug_session(
            request=SimpleNamespace(proxy_location=None),
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result.status_code == 202
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_prewarm_debug_session_treats_an_unresolved_binding_race_as_a_noop() -> None:
    app_mock = MagicMock()
    app_mock.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=True)
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(return_value=None)
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(
        side_effect=IntegrityError("INSERT", {}, Exception("duplicate live binding"))
    )

    with _patched_app(app_mock):
        result = await debug_sessions_mod.prewarm_debug_session(
            request=SimpleNamespace(proxy_location=None),
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result.status_code == 202
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_prewarm_debug_session_treats_creation_failure_as_a_noop() -> None:
    app_mock = MagicMock()
    app_mock.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=True)
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(return_value=None)
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(side_effect=RuntimeError("infra unavailable"))

    with _patched_app(app_mock):
        result = await debug_sessions_mod.prewarm_debug_session(
            request=SimpleNamespace(proxy_location=None),
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result.status_code == 202
    assert result.body == b""


@pytest.mark.asyncio
async def test_prewarm_debug_session_closes_an_unpublished_session_and_returns_accepted() -> None:
    browser_session = SimpleNamespace(persistent_browser_session_id="pbs_unpublished")
    app_mock = MagicMock()
    app_mock.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=True)
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(return_value=None)
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=browser_session)
    app_mock.DATABASE.browser_sessions.mark_prewarm_dispatched = AsyncMock(side_effect=RuntimeError("db unavailable"))
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()

    with _patched_app(app_mock):
        result = await debug_sessions_mod.prewarm_debug_session(
            request=SimpleNamespace(proxy_location=None),
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result.status_code == 202
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_awaited_once_with("org_123", "pbs_unpublished")


@pytest.mark.asyncio
async def test_prewarm_debug_session_closes_a_session_when_dispatch_loses_its_binding() -> None:
    browser_session = SimpleNamespace(persistent_browser_session_id="pbs_unpublished")
    app_mock = MagicMock()
    app_mock.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=True)
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(return_value=None)
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=browser_session)
    app_mock.DATABASE.browser_sessions.mark_prewarm_dispatched = AsyncMock(return_value=False)
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()

    with _patched_app(app_mock):
        result = await debug_sessions_mod.prewarm_debug_session(
            request=SimpleNamespace(proxy_location=None),
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result.status_code == 202
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_awaited_once_with("org_123", "pbs_unpublished")


@pytest.mark.parametrize("already_started", [False, True])
@pytest.mark.asyncio
async def test_get_debug_session_claims_a_compatible_prewarm(already_started: bool) -> None:
    claimed = SimpleNamespace(
        debug_session_id="ds_prewarm",
        browser_session_id="pbs_prewarm",
        pbs_browser_profile_id=None,
    )
    browser_session = SimpleNamespace(
        persistent_browser_session_id="pbs_prewarm",
        proxy_location=ProxyLocation.RESIDENTIAL,
        status="created",
        ip_address=None,
        started_at=(debug_sessions_mod.datetime.now(debug_sessions_mod.timezone.utc) if already_started else None),
        completed_at=None,
        created_at=debug_sessions_mod.datetime.now(debug_sessions_mod.timezone.utc),
        browser_profile_id=None,
        runnable_type=debug_sessions_mod.PREWARM_DISPATCHED_RUNNABLE_TYPE,
    )
    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=None)
    app_mock.DATABASE.debug.create_debug_session = AsyncMock(return_value=claimed)
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(
        return_value=browser_session
    )
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=browser_session)
    app_mock.DATABASE.browser_sessions.clear_prewarm_binding = AsyncMock(return_value=True)
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=SimpleNamespace(proxy_location=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.renew_or_close_session = AsyncMock(return_value=browser_session)
    app_mock.PERSISTENT_SESSIONS_MANAGER.seconds_until_fixed_deadline = AsyncMock(return_value=None)

    with _patched_app(app_mock), capture_logs() as logs:
        result = await debug_sessions_mod.get_or_create_debug_session_by_user_and_workflow_permanent_id(
            "wpid_test",
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result is claimed
    app_mock.DATABASE.debug.create_debug_session.assert_awaited_once_with(
        browser_session_id="pbs_prewarm",
        organization_id="org_123",
        user_id="user_123",
        workflow_permanent_id="wpid_test",
        vnc_streaming_supported=True,
    )
    # The editor's session count is cold-start editor creations plus these claims.
    [claim_line] = [log for log in logs if log["event"] == "Claimed prewarmed debug session"]
    assert (claim_line["session_kind"], claim_line["browser_session_id"]) == (BrowserSessionKind.editor, "pbs_prewarm")
    app_mock.AGENT_FUNCTION.supports_live_view.assert_not_called()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.assert_not_called()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session.assert_not_called()
    if already_started:
        app_mock.PERSISTENT_SESSIONS_MANAGER.renew_or_close_session.assert_awaited_once_with("pbs_prewarm", "org_123")
    else:
        app_mock.PERSISTENT_SESSIONS_MANAGER.renew_or_close_session.assert_not_called()


@pytest.mark.asyncio
async def test_claim_prewarm_reuses_debug_session_created_by_concurrent_request() -> None:
    browser_session = SimpleNamespace(
        persistent_browser_session_id="pbs_prewarm",
        proxy_location=ProxyLocation.RESIDENTIAL,
        browser_profile_id=None,
        started_at=None,
        runnable_type=debug_sessions_mod.PREWARM_DISPATCHED_RUNNABLE_TYPE,
    )
    claimed = SimpleNamespace(
        debug_session_id="ds_prewarm",
        browser_session_id="pbs_prewarm",
        pbs_browser_profile_id=None,
    )
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(
        return_value=browser_session
    )
    app_mock.DATABASE.browser_sessions.clear_prewarm_binding = AsyncMock(return_value=False)
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=browser_session)
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=claimed)
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=SimpleNamespace(proxy_location=None)
    )

    with _patched_app(app_mock):
        result = await debug_sessions_mod._claim_compatible_prewarm(
            workflow_permanent_id="wpid_test",
            organization_id="org_123",
            user_id="user_123",
        )

    assert result is claimed
    app_mock.DATABASE.debug.create_debug_session.assert_not_called()


@pytest.mark.asyncio
async def test_claim_prewarm_closes_the_browser_when_debug_session_creation_fails() -> None:
    browser_session = SimpleNamespace(
        persistent_browser_session_id="pbs_prewarm",
        proxy_location=ProxyLocation.RESIDENTIAL,
        ip_address=None,
        browser_profile_id=None,
        started_at=None,
        runnable_type=debug_sessions_mod.PREWARM_DISPATCHED_RUNNABLE_TYPE,
    )
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(
        return_value=browser_session
    )
    app_mock.DATABASE.browser_sessions.clear_prewarm_binding = AsyncMock(return_value=True)
    app_mock.DATABASE.debug.create_debug_session = AsyncMock(side_effect=RuntimeError("db unavailable"))
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=SimpleNamespace(proxy_location=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()

    with _patched_app(app_mock), pytest.raises(RuntimeError, match="db unavailable"):
        await debug_sessions_mod._claim_compatible_prewarm(
            workflow_permanent_id="wpid_test",
            organization_id="org_123",
            user_id="user_123",
        )

    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_awaited_once_with("org_123", "pbs_prewarm")


@pytest.mark.asyncio
async def test_claim_prewarm_rejects_a_started_session_that_cannot_be_renewed() -> None:
    browser_session = SimpleNamespace(
        persistent_browser_session_id="pbs_expiring",
        proxy_location=ProxyLocation.RESIDENTIAL,
        started_at=debug_sessions_mod.datetime.now(debug_sessions_mod.timezone.utc),
        runnable_type=debug_sessions_mod.PREWARM_DISPATCHED_RUNNABLE_TYPE,
    )
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(
        return_value=browser_session
    )
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=SimpleNamespace(proxy_location=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.renew_or_close_session = AsyncMock(
        side_effect=debug_sessions_mod.BrowserSessionNotRenewable("Session has expired", "pbs_expiring")
    )

    with _patched_app(app_mock):
        result = await debug_sessions_mod._claim_compatible_prewarm(
            workflow_permanent_id="wpid_test",
            organization_id="org_123",
            user_id="user_123",
        )

    assert result is None
    app_mock.DATABASE.browser_sessions.clear_prewarm_binding.assert_not_called()
    app_mock.DATABASE.debug.create_debug_session.assert_not_called()


@pytest.mark.asyncio
async def test_claim_prewarm_does_not_adopt_a_session_before_dispatch_finishes() -> None:
    browser_session = SimpleNamespace(
        persistent_browser_session_id="pbs_pending",
        proxy_location=ProxyLocation.RESIDENTIAL,
        runnable_type=debug_sessions_mod.PREWARM_PENDING_RUNNABLE_TYPE,
        started_at=None,
    )
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(
        return_value=browser_session
    )

    with _patched_app(app_mock):
        result = await debug_sessions_mod._claim_compatible_prewarm(
            workflow_permanent_id="wpid_test",
            organization_id="org_123",
            user_id="user_123",
        )

    assert result is None
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id.assert_not_called()
    app_mock.DATABASE.browser_sessions.clear_prewarm_binding.assert_not_called()
    app_mock.DATABASE.debug.create_debug_session.assert_not_called()


@pytest.mark.asyncio
async def test_claim_prewarm_retires_an_incompatible_proxy() -> None:
    browser_session = SimpleNamespace(
        persistent_browser_session_id="pbs_wrong_proxy",
        proxy_location=ProxyLocation.RESIDENTIAL_GB,
        runnable_type=debug_sessions_mod.PREWARM_DISPATCHED_RUNNABLE_TYPE,
        started_at=None,
    )
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(
        return_value=browser_session
    )
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=SimpleNamespace(proxy_location=ProxyLocation.RESIDENTIAL)
    )
    app_mock.DATABASE.browser_sessions.clear_prewarm_binding = AsyncMock(return_value=True)
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()

    with _patched_app(app_mock):
        result = await debug_sessions_mod._claim_compatible_prewarm(
            workflow_permanent_id="wpid_test",
            organization_id="org_123",
            user_id="user_123",
        )

    assert result is None
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_awaited_once_with("org_123", "pbs_wrong_proxy")
    app_mock.DATABASE.debug.create_debug_session.assert_not_called()


@pytest.mark.asyncio
async def test_claim_prewarm_retires_a_fixed_deadline_without_a_fresh_debug_window() -> None:
    browser_session = SimpleNamespace(
        persistent_browser_session_id="pbs_short_lived",
        proxy_location=ProxyLocation.RESIDENTIAL,
        runnable_type=debug_sessions_mod.PREWARM_DISPATCHED_RUNNABLE_TYPE,
        started_at=debug_sessions_mod.datetime.now(debug_sessions_mod.timezone.utc),
    )
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_live_bound_persistent_browser_session = AsyncMock(
        return_value=browser_session
    )
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=SimpleNamespace(proxy_location=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.renew_or_close_session = AsyncMock(return_value=browser_session)
    app_mock.PERSISTENT_SESSIONS_MANAGER.seconds_until_fixed_deadline = AsyncMock(return_value=5 * 60)
    app_mock.DATABASE.browser_sessions.clear_prewarm_binding = AsyncMock(return_value=True)
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()

    with _patched_app(app_mock):
        result = await debug_sessions_mod._claim_compatible_prewarm(
            workflow_permanent_id="wpid_test",
            organization_id="org_123",
            user_id="user_123",
        )

    assert result is None
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_awaited_once_with("org_123", "pbs_short_lived")
    app_mock.DATABASE.debug.create_debug_session.assert_not_called()


@pytest.mark.asyncio
async def test_get_debug_session_falls_back_when_prewarm_claim_fails() -> None:
    cold_session = SimpleNamespace(debug_session_id="ds_cold")
    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=None)

    with (
        _patched_app(app_mock),
        patch.object(
            debug_sessions_mod,
            "_claim_compatible_prewarm",
            AsyncMock(side_effect=RuntimeError("prewarm unavailable")),
        ),
        patch.object(
            debug_sessions_mod,
            "new_debug_session",
            AsyncMock(return_value=cold_session),
        ) as cold_start,
    ):
        result = await debug_sessions_mod.get_or_create_debug_session_by_user_and_workflow_permanent_id(
            "wpid_test",
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result is cold_session
    cold_start.assert_awaited_once()


@pytest.mark.parametrize(
    ("rollout_enabled", "workflow_proxy_location", "expected_proxy_location"),
    [
        (False, None, ProxyLocation.RESIDENTIAL),
        (True, None, ProxyLocation.NONE),
        (False, ProxyLocation.RESIDENTIAL_GB, ProxyLocation.RESIDENTIAL_GB),
        (False, ProxyLocation.NONE, ProxyLocation.NONE),
    ],
)
@pytest.mark.asyncio
async def test_new_debug_session_uses_workflow_proxy_default_for_created_browser(
    monkeypatch: pytest.MonkeyPatch,
    rollout_enabled: bool,
    workflow_proxy_location: ProxyLocation | None,
    expected_proxy_location: ProxyLocation,
) -> None:
    monkeypatch.setattr(settings, "RUNTIME_PROXY_DEFAULT_NONE_ENABLED", rollout_enabled)
    created_debug_session = SimpleNamespace(debug_session_id="ds_new", pbs_browser_profile_id=None)
    new_browser_session = SimpleNamespace(
        persistent_browser_session_id="pbs_new",
        ip_address=None,
        browser_address=None,
        browser_profile_id=None,
    )

    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=None)
    app_mock.DATABASE.debug.complete_debug_sessions = AsyncMock(
        return_value=[SimpleNamespace(browser_session_id="pbs_old")]
    )
    app_mock.DATABASE.debug.create_debug_session = AsyncMock(return_value=created_debug_session)
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock()
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=SimpleNamespace(proxy_location=workflow_proxy_location)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=new_browser_session)
    app_mock.AGENT_FUNCTION.supports_live_view = AsyncMock(return_value=True)
    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access = AsyncMock()

    with (
        _patched_app(app_mock),
        patch.object(debug_sessions_mod.settings, "ENV", "local"),
    ):
        result = await debug_sessions_mod.new_debug_session(
            "wpid_test",
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result is created_debug_session
    app_mock.AGENT_FUNCTION.validate_enterprise_feature_access.assert_not_awaited()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session.assert_not_awaited()
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_not_awaited()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session.assert_awaited_once_with(
        organization_id="org_123",
        timeout_minutes=debug_sessions_mod.settings.DEBUG_SESSION_TIMEOUT_MINUTES,
        proxy_location=expected_proxy_location,
        wait_for_startup=False,
        # A debug session exists to be watched in the studio, so it always declares it.
        needs_live_view=True,
        created_by="user_123",
        session_kind=BrowserSessionKind.editor,
        created_for_workflow_permanent_id="wpid_test",
    )
    app_mock.DATABASE.debug.create_debug_session.assert_awaited_once_with(
        browser_session_id="pbs_new",
        organization_id="org_123",
        user_id="user_123",
        workflow_permanent_id="wpid_test",
        vnc_streaming_supported=True,
    )


@pytest.mark.asyncio
async def test_new_debug_session_records_no_vnc_when_the_infrastructure_cannot_serve_it() -> None:
    """Asserted under the local carve-out, which is the most permissive left-hand side there is:
    where the browser runs still decides, or the studio offers a stream that fails on click."""
    created_debug_session = SimpleNamespace(debug_session_id="ds_new", pbs_browser_profile_id=None)
    new_browser_session = SimpleNamespace(
        persistent_browser_session_id="pbs_new",
        ip_address=None,
        browser_address="wss://session-router.example/pbs_new",
        browser_profile_id=None,
    )

    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=None)
    app_mock.DATABASE.debug.complete_debug_sessions = AsyncMock(return_value=[])
    app_mock.DATABASE.debug.create_debug_session = AsyncMock(return_value=created_debug_session)
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock()
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=SimpleNamespace(proxy_location=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=new_browser_session)
    app_mock.AGENT_FUNCTION.supports_live_view = AsyncMock(return_value=False)

    with (
        _patched_app(app_mock),
        patch.object(debug_sessions_mod.settings, "ENV", "local"),
    ):
        await debug_sessions_mod.new_debug_session(
            "wpid_test",
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert app_mock.DATABASE.debug.create_debug_session.await_args.kwargs["vnc_streaming_supported"] is False


@pytest.mark.parametrize(
    ("create_error", "expected_level"),
    [
        (BrowserSessionCreditAdmissionRefusal(), "warning"),
        (RuntimeError("infra unavailable"), "error"),
    ],
)
@pytest.mark.asyncio
async def test_new_debug_session_logs_a_credit_refusal_below_error_and_a_real_failure_at_error(
    create_error: Exception, expected_level: str
) -> None:
    """A 402 credit refusal is the caller's answer, not a defect; any other startup failure stays loud."""
    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=None)
    app_mock.DATABASE.debug.complete_debug_sessions = AsyncMock(return_value=[])
    app_mock.DATABASE.debug.create_debug_session = AsyncMock()
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=SimpleNamespace(proxy_location=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(side_effect=create_error)

    with (
        _patched_app(app_mock),
        patch.object(debug_sessions_mod.settings, "ENV", "local"),
        capture_logs() as logs,
        pytest.raises(type(create_error)) as raised,
    ):
        await debug_sessions_mod.new_debug_session(
            "wpid_test",
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert raised.value is create_error
    startup_logs = [entry for entry in logs if str(entry["event"]).startswith("Debug session browser startup")]
    assert [entry["log_level"] for entry in startup_logs] == [expected_level]
    assert startup_logs[0]["organization_id"] == "org_123"
    app_mock.DATABASE.debug.create_debug_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_hydrate_pbs_browser_profile_id_sets_field_when_session_exists() -> None:
    debug_session = SimpleNamespace(
        debug_session_id="ds_x",
        browser_session_id="pbs_x",
        pbs_browser_profile_id=None,
    )
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=SimpleNamespace(browser_profile_id="bp_42"),
    )

    with _patched_app(app_mock):
        result = await debug_sessions_mod._hydrate_pbs_browser_profile_id(
            debug_session,
            organization_id="org_x",
        )

    assert result is debug_session
    assert debug_session.pbs_browser_profile_id == "bp_42"


@pytest.mark.asyncio
async def test_hydrate_pbs_browser_profile_id_leaves_field_when_session_missing() -> None:
    debug_session = SimpleNamespace(
        debug_session_id="ds_x",
        browser_session_id="pbs_x",
        pbs_browser_profile_id=None,
    )
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=None,
    )

    with _patched_app(app_mock):
        result = await debug_sessions_mod._hydrate_pbs_browser_profile_id(
            debug_session,
            organization_id="org_x",
        )

    assert result is debug_session
    assert debug_session.pbs_browser_profile_id is None


@pytest.mark.asyncio
async def test_hydrate_pbs_browser_profile_id_swallows_lookup_errors() -> None:
    """A transient PBS lookup failure must not break the debug-session response.

    The UI degrades to "no profile" — the modal is informational and the
    backend still enforces the asymmetric run-path contract.
    """
    debug_session = SimpleNamespace(
        debug_session_id="ds_x",
        browser_session_id="pbs_x",
        pbs_browser_profile_id=None,
    )
    app_mock = MagicMock()
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        side_effect=RuntimeError("transient DB blip"),
    )

    with _patched_app(app_mock):
        result = await debug_sessions_mod._hydrate_pbs_browser_profile_id(
            debug_session,
            organization_id="org_x",
        )

    assert result is debug_session
    assert debug_session.pbs_browser_profile_id is None


@pytest.mark.asyncio
async def test_new_debug_session_response_carries_pbs_browser_profile_id() -> None:
    """Freshly created PBS rows already know their browser_profile_id; the POST
    handler must surface it so the FE can compute compatibility without an
    extra round-trip.
    """
    created_debug_session = SimpleNamespace(
        debug_session_id="ds_new",
        browser_session_id="pbs_new",
        pbs_browser_profile_id=None,
    )
    new_browser_session = SimpleNamespace(
        persistent_browser_session_id="pbs_new",
        ip_address=None,
        browser_address=None,
        browser_profile_id="bp_seeded",
    )

    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=None)
    app_mock.DATABASE.debug.complete_debug_sessions = AsyncMock(return_value=[])
    app_mock.DATABASE.debug.create_debug_session = AsyncMock(return_value=created_debug_session)
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock()
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=SimpleNamespace(proxy_location=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=new_browser_session)
    app_mock.AGENT_FUNCTION.supports_live_view = AsyncMock(return_value=True)

    with (
        _patched_app(app_mock),
        patch.object(debug_sessions_mod.settings, "ENV", "local"),
    ):
        result = await debug_sessions_mod.new_debug_session(
            "wpid_test",
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result is created_debug_session
    assert created_debug_session.pbs_browser_profile_id == "bp_seeded"


# ─── read-only viewer-state endpoint ───────────────────────────────────────


def _active_association() -> ActiveRunSessionAssociation:
    from datetime import datetime, timedelta, timezone

    return ActiveRunSessionAssociation(
        organization_id="org_x",
        workflow_permanent_id="wpid_x",
        debug_browser_session_id="pbs_debug",
        run_browser_session_id="pbs_run",
        workflow_run_id="wr_x",
        turn_id="turn_x",
        generation="generation_x",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def _viewer_state_app(*, run_status: str = "running") -> MagicMock:
    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(
        return_value=SimpleNamespace(browser_session_id="pbs_debug"),
    )
    app_mock.DATABASE.workflow_runs.get_workflow_run = AsyncMock(
        return_value=SimpleNamespace(
            workflow_permanent_id="wpid_x",
            browser_session_id="pbs_run",
            status=run_status,
        ),
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.renew_or_close_session = AsyncMock()
    return app_mock


@pytest.mark.asyncio
async def test_viewer_state_returns_matching_nonterminal_run_without_renewal() -> None:
    app_mock = _viewer_state_app()
    with (
        _patched_app(app_mock),
        patch.object(
            debug_sessions_mod,
            "get_active_run_session",
            AsyncMock(return_value=_active_association()),
        ),
    ):
        result = await debug_sessions_mod.get_debug_session_viewer_state(
            "wpid_x",
            current_org=SimpleNamespace(organization_id="org_x"),
            current_user_id="user_x",
        )

    assert result.active_run_session_id == "pbs_run"
    app_mock.PERSISTENT_SESSIONS_MANAGER.renew_or_close_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("association_update", "run_update"),
    [
        ({"organization_id": "org_other"}, {}),
        ({"workflow_permanent_id": "wpid_other"}, {}),
        ({"debug_browser_session_id": "pbs_other"}, {}),
        ({"run_browser_session_id": "pbs_other"}, {}),
        ({}, {"workflow_permanent_id": "wpid_other"}),
        ({}, {"browser_session_id": "pbs_other"}),
        ({}, {"status": "completed"}),
        ({}, {"status": "malformed"}),
    ],
)
async def test_viewer_state_rejects_mismatched_or_terminal_state(
    association_update: dict[str, str],
    run_update: dict[str, str],
) -> None:
    app_mock = _viewer_state_app()
    workflow_run = app_mock.DATABASE.workflow_runs.get_workflow_run.return_value
    for field, value in run_update.items():
        setattr(workflow_run, field, value)
    association = _active_association().model_copy(update=association_update)

    with (
        _patched_app(app_mock),
        patch.object(
            debug_sessions_mod,
            "get_active_run_session",
            AsyncMock(return_value=association),
        ),
    ):
        result = await debug_sessions_mod.get_debug_session_viewer_state(
            "wpid_x",
            current_org=SimpleNamespace(organization_id="org_x"),
            current_user_id="user_x",
        )

    assert result.active_run_session_id is None


@pytest.mark.asyncio
async def test_viewer_state_returns_null_for_missing_or_unreadable_association() -> None:
    for association_result in (None, RuntimeError("cache unavailable")):
        app_mock = _viewer_state_app()
        lookup = (
            AsyncMock(side_effect=association_result)
            if isinstance(association_result, Exception)
            else AsyncMock(return_value=association_result)
        )
        with (
            _patched_app(app_mock),
            patch.object(debug_sessions_mod, "get_active_run_session", lookup),
        ):
            result = await debug_sessions_mod.get_debug_session_viewer_state(
                "wpid_x",
                current_org=SimpleNamespace(organization_id="org_x"),
                current_user_id="user_x",
            )

        assert result.active_run_session_id is None
        app_mock.DATABASE.workflow_runs.get_workflow_run.assert_not_awaited()


# ─── login-block-compatibility endpoint ────────────────────────────────────


def _login_block(*, label: str = "login_1") -> SimpleNamespace:
    """A minimal stand-in for `LoginBlock`. The endpoint uses `isinstance` to
    branch, so we keep tests honest by patching `LoginBlock` to a sentinel
    class the namespace instance satisfies."""

    return SimpleNamespace(label=label)


def _wf(*, blocks: list) -> SimpleNamespace:
    return SimpleNamespace(workflow_definition=SimpleNamespace(blocks=blocks))


@pytest.mark.asyncio
async def test_compatibility_returns_compatible_when_no_debug_session() -> None:
    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=None)

    with _patched_app(app_mock):
        result = await debug_sessions_mod.get_login_block_compatibility(
            workflow_permanent_id="wpid_test",
            block_label="login_1",
            current_org=SimpleNamespace(organization_id="org_x"),
            current_user_id="user_x",
        )

    assert result.compatible is True
    assert result.reason is None
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id.assert_not_called()


@pytest.mark.asyncio
async def test_compatibility_returns_compatible_when_block_is_not_login() -> None:
    debug_session = SimpleNamespace(browser_session_id="pbs_x")
    non_login_block = SimpleNamespace(label="login_1")  # not a LoginBlock instance
    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=debug_session)
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=_wf(blocks=[non_login_block]),
    )

    with _patched_app(app_mock):
        result = await debug_sessions_mod.get_login_block_compatibility(
            workflow_permanent_id="wpid_test",
            block_label="login_1",
            current_org=SimpleNamespace(organization_id="org_x"),
            current_user_id="user_x",
        )

    assert result.compatible is True
    assert result.reason is None


@pytest.mark.asyncio
async def test_compatibility_compatible_when_credential_has_no_profile() -> None:
    debug_session = SimpleNamespace(browser_session_id="pbs_x")
    login_block = _login_block()
    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=debug_session)
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=_wf(blocks=[login_block]),
    )
    app_mock.WORKFLOW_SERVICE.resolve_login_block_browser_profile_id_pre_run = AsyncMock(
        return_value=None,
    )

    with _patched_app(app_mock):
        with patch.object(debug_sessions_mod, "LoginBlock", SimpleNamespace):
            result = await debug_sessions_mod.get_login_block_compatibility(
                workflow_permanent_id="wpid_test",
                block_label="login_1",
                current_org=SimpleNamespace(organization_id="org_x"),
                current_user_id="user_x",
            )

    assert result.compatible is True
    assert result.reason is None
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session.assert_not_called()


@pytest.mark.asyncio
async def test_compatibility_reports_pbs_no_profile_when_pbs_unprofiled() -> None:
    debug_session = SimpleNamespace(browser_session_id="pbs_x")
    login_block = _login_block()
    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=debug_session)
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=_wf(blocks=[login_block]),
    )
    app_mock.WORKFLOW_SERVICE.resolve_login_block_browser_profile_id_pre_run = AsyncMock(
        return_value="bp_cred",
    )
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=SimpleNamespace(browser_profile_id=None),
    )

    with _patched_app(app_mock):
        with patch.object(debug_sessions_mod, "LoginBlock", SimpleNamespace):
            result = await debug_sessions_mod.get_login_block_compatibility(
                workflow_permanent_id="wpid_test",
                block_label="login_1",
                current_org=SimpleNamespace(organization_id="org_x"),
                current_user_id="user_x",
            )

    assert result.compatible is False
    assert result.reason == "pbs_no_profile"


@pytest.mark.asyncio
async def test_compatibility_reports_pbs_different_profile_when_profiles_differ() -> None:
    debug_session = SimpleNamespace(browser_session_id="pbs_x")
    login_block = _login_block()
    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=debug_session)
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=_wf(blocks=[login_block]),
    )
    app_mock.WORKFLOW_SERVICE.resolve_login_block_browser_profile_id_pre_run = AsyncMock(
        return_value="bp_cred",
    )
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=SimpleNamespace(browser_profile_id="bp_other"),
    )

    with _patched_app(app_mock):
        with patch.object(debug_sessions_mod, "LoginBlock", SimpleNamespace):
            result = await debug_sessions_mod.get_login_block_compatibility(
                workflow_permanent_id="wpid_test",
                block_label="login_1",
                current_org=SimpleNamespace(organization_id="org_x"),
                current_user_id="user_x",
            )

    assert result.compatible is False
    assert result.reason == "pbs_different_profile"


@pytest.mark.asyncio
async def test_compatibility_reports_compatible_when_profiles_match() -> None:
    debug_session = SimpleNamespace(browser_session_id="pbs_x")
    login_block = _login_block()
    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=debug_session)
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=_wf(blocks=[login_block]),
    )
    app_mock.WORKFLOW_SERVICE.resolve_login_block_browser_profile_id_pre_run = AsyncMock(
        return_value="bp_same",
    )
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
        return_value=SimpleNamespace(browser_profile_id="bp_same"),
    )

    with _patched_app(app_mock):
        with patch.object(debug_sessions_mod, "LoginBlock", SimpleNamespace):
            result = await debug_sessions_mod.get_login_block_compatibility(
                workflow_permanent_id="wpid_test",
                block_label="login_1",
                current_org=SimpleNamespace(organization_id="org_x"),
                current_user_id="user_x",
            )

    assert result.compatible is True
    assert result.reason is None


@pytest.mark.asyncio
async def test_compatibility_returns_compatible_when_block_label_not_found() -> None:
    debug_session = SimpleNamespace(browser_session_id="pbs_x")
    other_block = SimpleNamespace(label="other_block")
    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(return_value=debug_session)
    app_mock.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(
        return_value=_wf(blocks=[other_block]),
    )

    with _patched_app(app_mock):
        result = await debug_sessions_mod.get_login_block_compatibility(
            workflow_permanent_id="wpid_test",
            block_label="login_missing",
            current_org=SimpleNamespace(organization_id="org_x"),
            current_user_id="user_x",
        )

    assert result.compatible is True
    assert result.reason is None
    app_mock.WORKFLOW_SERVICE.resolve_login_block_browser_profile_id_pre_run.assert_not_called()


_OWNER_ORG = Organization(
    organization_id="o_owner",
    organization_name="Owner",
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
    modified_at=datetime(2026, 1, 1, tzinfo=UTC),
)


async def _debug_session_owner(
    monkeypatch: pytest.MonkeyPatch, principal: RequestPrincipal, headers: dict[str, str]
) -> tuple[int, list[str]]:
    """GET the mounted debug-session route; the principal becomes readable only once get_current_org has run."""
    resolved: list[RequestPrincipal] = []

    async def current_org() -> Organization:
        resolved.append(principal)
        return _OWNER_ORG

    async def authenticate_user(token: str) -> str | None:
        return "user_member" if token == "valid-bearer" else None

    owners: list[str] = []

    async def get_debug_session(*, organization_id: str, user_id: str, workflow_permanent_id: str) -> DebugSession:
        owners.append(user_id)
        now = datetime.now(UTC)
        return DebugSession(
            debug_session_id="ds_1",
            browser_session_id="pbs_A",
            workflow_permanent_id=workflow_permanent_id,
            user_id=user_id,
            created_at=now,
            modified_at=now,
            status="created",
        )

    monkeypatch.setattr(org_auth_service, "get_request_principal", lambda: resolved[-1] if resolved else None)
    monkeypatch.setattr(org_auth_service, "get_current_org_cached", AsyncMock(return_value=_OWNER_ORG))
    monkeypatch.setattr(org_auth_service.app, "authenticate_user_function", authenticate_user)
    app_mock = MagicMock()
    app_mock.DATABASE.debug.get_debug_session = AsyncMock(side_effect=get_debug_session)
    app_mock.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=None)
    app_mock.PERSISTENT_SESSIONS_MANAGER.renew_or_close_session = AsyncMock()
    fastapi_app = FastAPI()
    fastapi_app.add_api_route(
        "/debug-session/{workflow_permanent_id}",
        debug_sessions_mod.get_or_create_debug_session_by_user_and_workflow_permanent_id,
        methods=["GET"],
    )
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = current_org
    with _patched_app(app_mock):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test") as client:
            response = await client.get("/debug-session/wpid_test", headers=headers)
    return response.status_code, owners


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("principal", "headers", "expected"),
    [
        (RequestPrincipal("o_owner", AuthKind.api_key), {"x-api-key": "org-key"}, (200, ["o_owner_user"])),
        (
            RequestPrincipal("o_owner", AuthKind.api_key),
            {"x-api-key": "org-key", "x-user-agent": "skyvern-ui"},
            (200, ["o_owner_user"]),
        ),
        (
            RequestPrincipal("o_owner", AuthKind.ui_session),
            {"x-api-key": "ui-session-key", "x-user-agent": "skyvern-ui"},
            (200, ["o_owner_user"]),
        ),
        (
            RequestPrincipal("o_owner", AuthKind.member_api_key, "user_member", "org:member"),
            {"x-api-key": "member-key"},
            (200, ["user_member"]),
        ),
        (
            RequestPrincipal("o_owner", AuthKind.bearer, "user_member", "org:member"),
            {"Authorization": "Bearer valid-bearer"},
            (200, ["user_member"]),
        ),
        (RequestPrincipal("o_owner", AuthKind.scoped_api_key), {"x-api-key": "scoped-key"}, (403, [])),
        (
            RequestPrincipal("o_owner", AuthKind.scoped_api_key),
            {"x-api-key": "scoped-key", "x-user-agent": "skyvern-ui"},
            (403, []),
        ),
        (
            RequestPrincipal("o_owner", AuthKind.scoped_api_key),
            {"x-api-key": "scoped-key", "Authorization": "Bearer invalid-bearer"},
            (403, []),
        ),
        (
            RequestPrincipal("o_owner", AuthKind.scoped_api_key),
            {"x-api-key": "scoped-key", "Authorization": "Bearer valid-bearer"},
            (403, []),
        ),
        (
            RequestPrincipal("o_owner", AuthKind.scoped_api_key_and_bearer, "user_member", "org:member"),
            {"x-api-key": "scoped-key", "Authorization": "Bearer valid-bearer"},
            (200, ["user_member"]),
        ),
    ],
    ids=[
        "org_key_without_ui_user_agent",
        "org_key_with_ui_user_agent",
        "studio_ui_session_key",
        "member_key",
        "member_bearer",
        "scoped_key_without_user_agent",
        "scoped_key_with_ui_user_agent",
        "scoped_key_with_invalid_bearer",
        "scoped_key_with_a_valid_bearer_not_verified_for_the_org",
        "scoped_key_with_verified_bearer",
    ],
)
async def test_debug_session_owner_for_each_credential_kind(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
    headers: dict[str, str],
    expected: tuple[int, list[str]],
) -> None:
    assert await _debug_session_owner(monkeypatch, principal, headers) == expected

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from skyvern.config import settings
from skyvern.forge.sdk.copilot.active_run_session import ActiveRunSessionAssociation
from skyvern.forge.sdk.routes import debug_sessions as debug_sessions_mod
from skyvern.schemas.runs import ProxyLocation


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
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=browser_session)

    with patch.object(debug_sessions_mod, "app", app_mock):
        result = await debug_sessions_mod.prewarm_debug_session(
            request=SimpleNamespace(proxy_location=None),
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result.status_code == 202
    assert result.body == b""
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
        result = await debug_sessions_mod.prewarm_debug_session(
            request=SimpleNamespace(proxy_location=None),
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result.status_code == 202
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session.assert_awaited_once_with("org_123", "pbs_unpublished")


def test_prewarm_bound_key_is_unambiguous() -> None:
    assert debug_sessions_mod._prewarm_bound_key("a:b", "c") != debug_sessions_mod._prewarm_bound_key("a", "b:c")


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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock), pytest.raises(RuntimeError, match="db unavailable"):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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
        patch.object(debug_sessions_mod, "app", app_mock),
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

    with (
        patch.object(debug_sessions_mod, "app", app_mock),
        patch.object(debug_sessions_mod.settings, "ENV", "local"),
    ):
        result = await debug_sessions_mod.new_debug_session(
            "wpid_test",
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert result is created_debug_session
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
        patch.object(debug_sessions_mod, "app", app_mock),
        patch.object(debug_sessions_mod.settings, "ENV", "local"),
    ):
        await debug_sessions_mod.new_debug_session(
            "wpid_test",
            current_org=SimpleNamespace(organization_id="org_123"),
            current_user_id="user_123",
        )

    assert app_mock.DATABASE.debug.create_debug_session.await_args.kwargs["vnc_streaming_supported"] is False


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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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
        patch.object(debug_sessions_mod, "app", app_mock),
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
        patch.object(debug_sessions_mod, "app", app_mock),
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
        patch.object(debug_sessions_mod, "app", app_mock),
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
            patch.object(debug_sessions_mod, "app", app_mock),
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
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

    with patch.object(debug_sessions_mod, "app", app_mock):
        result = await debug_sessions_mod.get_login_block_compatibility(
            workflow_permanent_id="wpid_test",
            block_label="login_missing",
            current_org=SimpleNamespace(organization_id="org_x"),
            current_user_id="user_x",
        )

    assert result.compatible is True
    assert result.reason is None
    app_mock.WORKFLOW_SERVICE.resolve_login_block_browser_profile_id_pre_run.assert_not_called()

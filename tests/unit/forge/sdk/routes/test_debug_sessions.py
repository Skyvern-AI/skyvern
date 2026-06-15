from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.routes import debug_sessions as debug_sessions_mod


@pytest.mark.asyncio
async def test_new_debug_session_skips_redundant_local_close_before_create() -> None:
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
        return_value=SimpleNamespace(proxy_location=None)
    )
    app_mock.PERSISTENT_SESSIONS_MANAGER.close_session = AsyncMock()
    app_mock.PERSISTENT_SESSIONS_MANAGER.create_session = AsyncMock(return_value=new_browser_session)

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
        proxy_location=debug_sessions_mod.ProxyLocation.RESIDENTIAL,
        wait_for_startup=False,
    )
    app_mock.DATABASE.debug.create_debug_session.assert_awaited_once_with(
        browser_session_id="pbs_new",
        organization_id="org_123",
        user_id="user_123",
        workflow_permanent_id="wpid_test",
        vnc_streaming_supported=True,
    )


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

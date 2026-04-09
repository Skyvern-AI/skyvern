"""
Tests for RealBrowserManager cache behavior (regression coverage for PR #9020).

PR #9020 introduced a regression where the self.pages cache check was gated
behind `if not browser_session_id:`, causing PBS workflow runs to skip the cache
on every call and re-invoke navigate_to_url() on every step.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.real_browser_manager import RealBrowserManager


def make_workflow_run(
    workflow_run_id: str,
    parent_workflow_run_id: str | None = None,
    organization_id: str = "org_test",
    browser_profile_id: str | None = None,
) -> MagicMock:
    wfr = MagicMock()
    wfr.workflow_run_id = workflow_run_id
    wfr.parent_workflow_run_id = parent_workflow_run_id
    wfr.organization_id = organization_id
    wfr.browser_profile_id = browser_profile_id
    wfr.proxy_location = None
    wfr.extra_http_headers = None
    wfr.browser_address = None
    return wfr


@pytest.mark.asyncio
async def test_pbs_workflow_run_cache_hit_on_second_call() -> None:
    """PBS runs must hit the cache on subsequent calls and NOT re-enter the PBS branch."""
    manager = RealBrowserManager()
    cached_state = MagicMock()
    manager.pages["wfr_child"] = cached_state

    workflow_run = make_workflow_run("wfr_child")
    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        result = await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id="bs_123",
        )
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.assert_not_called()

    assert result is cached_state


@pytest.mark.asyncio
async def test_pbs_workflow_run_does_not_inherit_parent_browser() -> None:
    """Child PBS runs must NOT inherit the parent's browser on the first call."""
    manager = RealBrowserManager()
    parent_state = MagicMock()
    manager.pages["wfr_parent"] = parent_state

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")

    pbs_state = MagicMock()
    pbs_state.get_working_page = AsyncMock(return_value=None)
    pbs_state.get_or_create_page = AsyncMock()

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=pbs_state)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        result = await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id="bs_123",
        )

    # Must use the PBS session, not the parent's browser
    assert result is pbs_state
    assert result is not parent_state


@pytest.mark.asyncio
async def test_pbs_workflow_run_returns_own_cache_not_parent() -> None:
    """When both child and parent are cached, PBS must return the child's own entry."""
    manager = RealBrowserManager()
    child_state = MagicMock()
    manager.pages["wfr_child"] = child_state
    manager.pages["wfr_parent"] = MagicMock()

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")
    result = await manager.get_or_create_for_workflow_run(
        workflow_run=workflow_run,
        url="https://example.com",
        browser_session_id="bs_123",
    )

    assert result is child_state


@pytest.mark.asyncio
async def test_non_pbs_workflow_run_cache_hit_on_second_call() -> None:
    """Non-PBS runs must also hit the early cache check on subsequent calls."""
    manager = RealBrowserManager()
    cached_state = MagicMock()
    manager.pages["wfr_child"] = cached_state

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")
    result = await manager.get_or_create_for_workflow_run(
        workflow_run=workflow_run,
        url=None,
        browser_session_id=None,
    )

    assert result is cached_state


@pytest.mark.asyncio
async def test_non_pbs_workflow_run_inherits_parent_browser() -> None:
    """Non-PBS child runs must still inherit the parent's browser when no browser_session_id."""
    manager = RealBrowserManager()
    parent_state = MagicMock()
    manager.pages["wfr_parent"] = parent_state

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")

    result = await manager.get_or_create_for_workflow_run(
        workflow_run=workflow_run,
        url=None,
        browser_session_id=None,
    )

    assert result is parent_state
    # Both entries should be synced
    assert manager.pages["wfr_child"] is parent_state
    assert manager.pages["wfr_parent"] is parent_state

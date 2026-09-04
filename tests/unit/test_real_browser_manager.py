"""
Tests for RealBrowserManager cache behavior (regression coverage for PR #9020).

PR #9020 introduced a regression where the self.pages cache check was gated
behind `if not browser_session_id:`, causing PBS workflow runs to skip the cache
on every call and re-invoke navigate_to_url() on every step.
"""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from skyvern.exceptions import BrowserSessionAlreadyOccupiedError, MissingBrowserStateForBrowserSession
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.artifact.storage.recording_test_helpers import fake_prepared_recording
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.streaming import registries
from skyvern.webeye import real_browser_manager
from skyvern.webeye.browser_artifacts import (
    BrowserArtifacts,
    DownloadBinding,
    RecordingPrefixSnapshot,
    VideoArtifact,
)
from skyvern.webeye.browser_engine import (
    BrowserEngineBootstrapError,
    BrowserEngineMetadata,
    BrowserEngineSelection,
)
from skyvern.webeye.browser_factory import set_popup_video_listener
from skyvern.webeye.browser_retirement import BrowserStatePublicationRejected
from skyvern.webeye.real_browser_manager import RealBrowserManager, _PersistentSessionLease
from skyvern.webeye.real_browser_state import RealBrowserState


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


def configure_browser_context_acquired_hook(mock_app: MagicMock) -> None:
    mock_app.AGENT_FUNCTION.on_browser_context_acquired = AsyncMock()
    mock_app.DATABASE.browser_sessions.touch_last_activity = AsyncMock()


class _StopBeforeBrowserContext(Exception):
    pass


@pytest.mark.asyncio
async def test_task_first_creation_gives_engine_flag_the_pinned_workflow_id() -> None:
    """A workflow-owned task that creates the browser first pins under workflow_run_id but keeps it
    out of browser-context creation (download-dir scoping). The engine-flag context must still carry
    that workflow_run_id — so both the flag distinct_id and its workflow_run_id property match the
    pinned run — while the browser context keeps the raw (None) workflow_run_id."""
    manager = RealBrowserManager()
    seen: dict[str, object] = {}

    async def capture(*, run_key: str | None, context: object) -> object:
        seen["run_key"] = run_key
        seen["context"] = context
        raise _StopBeforeBrowserContext

    with patch.object(manager, "get_or_resolve_engine_selection", side_effect=capture):
        with pytest.raises(_StopBeforeBrowserContext):
            await manager._create_browser_state(
                task_id="tsk_1",
                workflow_run_id=None,  # kept out of browser-context creation (download-dir scoping)
                engine_run_key="wr_1",
                engine_workflow_run_id="wr_1",
            )

    assert seen["run_key"] == "wr_1"
    assert seen["context"].workflow_run_id == "wr_1"  # engine flag sees the pinned workflow run
    assert seen["context"].task_id == "tsk_1"


@pytest.mark.asyncio
async def test_pbs_workflow_run_cache_hit_on_second_call() -> None:
    """PBS runs must hit the cache on subsequent calls and NOT re-enter the PBS branch."""
    manager = RealBrowserManager()
    cached_state = MagicMock()
    manager.pages["wfr_child"] = cached_state

    workflow_run = make_workflow_run("wfr_child")
    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
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
        configure_browser_context_acquired_hook(mock_app)
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
async def test_pbs_workflow_run_with_synthetic_run_does_not_assert_session_ownership() -> None:
    """SKY-13518: a synthetic bookkeeping run (minted per-action by run_sdk_action) never begins the
    session, so presenting it as the expected owner can only ever fail the DB ownership guard."""
    manager = RealBrowserManager()
    workflow_run = make_workflow_run("wfr_synthetic")

    pbs_state = MagicMock()
    pbs_state.get_working_page = AsyncMock(return_value=None)
    pbs_state.get_or_create_page = AsyncMock()

    skyvern_context.set(SkyvernContext(workflow_run_id="wfr_synthetic", workflow_run_is_synthetic=True))
    try:
        with (
            patch("skyvern.webeye.real_browser_manager.app") as mock_app,
            patch(
                "skyvern.webeye.real_browser_manager._rebind_pbs_download_dir",
                new_callable=AsyncMock,
            ) as mock_rebind,
        ):
            configure_browser_context_acquired_hook(mock_app)
            mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=pbs_state)
            mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

            await manager.get_or_create_for_workflow_run(
                workflow_run=workflow_run,
                url="https://example.com",
                browser_session_id="bs_123",
            )
    finally:
        skyvern_context.reset()

    call_kwargs = mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.await_args.kwargs
    assert call_kwargs["expected_runnable_id"] is None
    # A synthetic run only reads a session owned by another runnable. It must not change that
    # runnable's download destination or acquire a lease that could later release the session.
    mock_rebind.assert_not_awaited()
    assert "wfr_synthetic" not in manager._persistent_session_leases


@pytest.mark.asyncio
async def test_pbs_workflow_run_without_lease_still_asserts_run_as_expected_owner() -> None:
    """Without the synthetic marker the run-id fallback stays: cross-process reads must keep being
    governed by the DB ownership guard (SKY-13473)."""
    manager = RealBrowserManager()
    workflow_run = make_workflow_run("wfr_child")

    pbs_state = MagicMock()
    pbs_state.get_working_page = AsyncMock(return_value=None)
    pbs_state.get_or_create_page = AsyncMock()

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=pbs_state)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id="bs_123",
        )

    call_kwargs = mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.await_args.kwargs
    assert call_kwargs["expected_runnable_id"] == "wfr_child"


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
    """Non-PBS child runs must still inherit a healthy parent browser when no browser_session_id."""
    manager = RealBrowserManager()
    parent_state = MagicMock()
    parent_state.get_working_page = AsyncMock(return_value=MagicMock())
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


@pytest.mark.asyncio
async def test_child_run_does_not_adopt_stale_sibling_browser_without_page() -> None:
    """A parent_workflow_run_id entry is shared by every child run of the same parent.

    When independent child runs are dispatched to one long-lived worker (e.g. a
    sequential fan-out), a later child can find an earlier, already-completed
    sibling's torn-down browser under the parent key. It must NOT be adopted:
    its page is gone, so the first browser block would raise
    ``MissingBrowserStatePage`` ("Browser state page is missing"). The manager
    must instead evict the stale entry and create a fresh browser for the run.
    """
    manager = RealBrowserManager()
    # Sibling C1 completed and left a torn-down browser under the shared parent key.
    stale_state = MagicMock()
    stale_state.get_working_page = AsyncMock(return_value=None)
    stale_state.is_connected = MagicMock(return_value=False)
    manager.pages["wfr_parent"] = stale_state

    workflow_run = make_workflow_run("wfr_child_2", parent_workflow_run_id="wfr_parent")

    fresh_state = MagicMock()
    fresh_state.get_or_create_page = AsyncMock()

    with patch.object(manager, "_create_browser_state", new=AsyncMock(return_value=fresh_state)) as mock_create:
        result = await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id=None,
        )

    # A brand-new browser must be created, not the stale sibling state.
    mock_create.assert_awaited_once()
    assert result is fresh_state
    assert result is not stale_state
    # The fresh browser gets a page (the parent early-return path skips this).
    fresh_state.get_or_create_page.assert_awaited_once()
    # The stale entry must be replaced by the fresh browser, not left dangling.
    assert manager.pages["wfr_child_2"] is fresh_state
    assert manager.pages["wfr_parent"] is fresh_state


@pytest.mark.asyncio
async def test_child_run_recovers_live_inherited_browser_without_page() -> None:
    """A ``use_parent_browser_session`` child inherits the parent's in-memory browser via the
    shared parent key. When that browser is still live but its last valid tab was closed,
    ``get_working_page()`` returns ``None`` even though the context is connected. The manager
    must recreate a page in the SAME context (preserving the parent's cookies/session) instead
    of evicting the live browser and starting a fresh one — which would orphan the parent's
    browser and lose its session.
    """
    manager = RealBrowserManager()
    # Parent's live in-memory browser, currently tab-less (last page was closed).
    live_state = MagicMock()
    live_state.get_working_page = AsyncMock(side_effect=[None, MagicMock()])
    live_state.is_connected = MagicMock(return_value=True)
    # A genuinely live tab-less context answers the bounded read-only liveness probe.
    live_state.browser_context = MagicMock()
    live_state.browser_context.cookies = AsyncMock(return_value=[])
    live_state.get_or_create_page = AsyncMock()
    manager.pages["wfr_parent"] = live_state

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")

    with patch.object(manager, "_create_browser_state", new=AsyncMock()) as mock_create:
        result = await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id=None,
        )

    # The live inherited browser is adopted, not evicted.
    assert result is live_state
    mock_create.assert_not_awaited()
    # A page is recreated in the existing context so the parent's session is preserved.
    live_state.get_or_create_page.assert_awaited_once()
    # Both entries still point at the live browser so it stays tracked for cleanup.
    assert manager.pages["wfr_child"] is live_state
    assert manager.pages["wfr_parent"] is live_state


class _FakeDriverClosedError(Exception):
    """Production-shaped closed-transport error: a dead Playwright/Patchright driver pipe
    surfaces this message on the first round-trip after the connection silently died."""


class TargetClosedError(Exception):
    """Named to match Playwright/Patchright ``TargetClosedError`` by type name only.

    ``scripts/patch_browser.sh`` rewrites playwright -> patchright for the agent image but not
    for ``cloud/persistent_browsers``, so closed-transport classification must match by type
    name/message, never by imported class identity.
    """


@pytest.mark.asyncio
async def test_child_run_does_not_adopt_inherited_browser_with_dead_transport() -> None:
    """SKY-13389: ``is_connected()`` only reads cached client-side flags, so an inherited browser
    whose driver/CDP transport died while idle (a long sequential-gate wait, a reaped remote
    browser, a TCP half-close) still reports connected. Taking the #14311 same-context recovery
    then runs ``new_page()`` on a dead transport and crashes the run with
    "Connection closed while reading from the driver". The manager must actively probe the
    transport before same-context recovery and, on a dead transport, evict + create a fresh
    browser instead.
    """
    manager = RealBrowserManager()
    # Inherited parent-key browser: page-less, reports connected (cached false positive),
    # but every real round-trip over its dead driver raises the closed-transport error.
    dead_state = MagicMock()
    dead_state.get_working_page = AsyncMock(return_value=None)
    dead_state.is_connected = MagicMock(return_value=True)
    dead_state.browser_context = MagicMock()
    dead_state.browser_context.cookies = AsyncMock(
        side_effect=_FakeDriverClosedError("BrowserContext.cookies: Connection closed while reading from the driver")
    )
    # If the pre-fix #14311 path is taken, new_page() inside get_or_create_page raises the same
    # error and crashes the run (reproducing the production crash signature).
    dead_state.get_or_create_page = AsyncMock(
        side_effect=_FakeDriverClosedError("BrowserContext.new_page: Connection closed while reading from the driver")
    )
    manager.pages["wfr_parent"] = dead_state

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")
    fresh_state = MagicMock()
    fresh_state.get_or_create_page = AsyncMock()

    with patch.object(manager, "_create_browser_state", new=AsyncMock(return_value=fresh_state)) as mock_create:
        result = await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id=None,
        )

    # A fresh browser is created; the dead browser is never recovered in-place.
    mock_create.assert_awaited_once()
    assert result is fresh_state
    assert result is not dead_state
    dead_state.get_or_create_page.assert_not_awaited()
    assert manager.pages["wfr_child"] is fresh_state
    assert manager.pages["wfr_parent"] is fresh_state


@pytest.mark.asyncio
async def test_child_run_evicts_inherited_browser_when_transport_probe_times_out() -> None:
    """A dead transport can hang rather than error; the bounded probe must time out and classify
    the inherited browser disconnected so the run gets a fresh browser instead of stalling."""
    manager = RealBrowserManager()
    dead_state = MagicMock()
    dead_state.get_working_page = AsyncMock(return_value=None)
    dead_state.is_connected = MagicMock(return_value=True)

    async def _hang(*args: object, **kwargs: object) -> list:
        await asyncio.sleep(1)
        return []

    dead_state.browser_context = MagicMock()
    dead_state.browser_context.cookies = _hang
    dead_state.get_or_create_page = AsyncMock()
    manager.pages["wfr_parent"] = dead_state

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")
    fresh_state = MagicMock()
    fresh_state.get_or_create_page = AsyncMock()

    with (
        patch.object(real_browser_manager, "_INHERITED_BROWSER_LIVENESS_PROBE_TIMEOUT_SECONDS", 0.01),
        patch.object(manager, "_create_browser_state", new=AsyncMock(return_value=fresh_state)) as mock_create,
    ):
        result = await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id=None,
        )

    mock_create.assert_awaited_once()
    assert result is fresh_state
    dead_state.get_or_create_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_child_run_evicts_inherited_browser_on_target_closed_by_name() -> None:
    """Closed-transport is classified by exception type NAME, not imported identity, because
    scripts/patch_browser.sh swaps playwright -> patchright for the agent image only. A
    TargetClosedError from either package (message alone not matching) still evicts + goes fresh."""
    manager = RealBrowserManager()
    dead_state = MagicMock()
    dead_state.get_working_page = AsyncMock(return_value=None)
    dead_state.is_connected = MagicMock(return_value=True)
    dead_state.browser_context = MagicMock()
    dead_state.browser_context.cookies = AsyncMock(side_effect=TargetClosedError("boom"))
    dead_state.get_or_create_page = AsyncMock()
    manager.pages["wfr_parent"] = dead_state

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")
    fresh_state = MagicMock()
    fresh_state.get_or_create_page = AsyncMock()

    with patch.object(manager, "_create_browser_state", new=AsyncMock(return_value=fresh_state)) as mock_create:
        result = await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id=None,
        )

    mock_create.assert_awaited_once()
    assert result is fresh_state
    dead_state.get_or_create_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_child_run_reraises_unexpected_probe_error() -> None:
    """An unexpected (non closed-transport) probe error must propagate, never be silently
    converted into a fresh-browser fallback that could mask a real defect."""
    manager = RealBrowserManager()
    state = MagicMock()
    state.get_working_page = AsyncMock(return_value=None)
    state.is_connected = MagicMock(return_value=True)
    state.browser_context = MagicMock()
    state.browser_context.cookies = AsyncMock(side_effect=ValueError("unexpected probe failure"))
    manager.pages["wfr_parent"] = state

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")

    with patch.object(manager, "_create_browser_state", new=AsyncMock()) as mock_create:
        with pytest.raises(ValueError, match="unexpected probe failure"):
            await manager.get_or_create_for_workflow_run(
                workflow_run=workflow_run,
                url="https://example.com",
                browser_session_id=None,
            )

    mock_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_inherited_browser_transport_alive_skips_probe_when_already_disconnected() -> None:
    state = MagicMock()
    state.is_connected = MagicMock(return_value=False)
    state.browser_context = MagicMock()
    state.browser_context.cookies = AsyncMock()

    assert await real_browser_manager._inherited_browser_transport_alive(state) is False
    state.browser_context.cookies.assert_not_awaited()


@pytest.mark.asyncio
async def test_inherited_browser_transport_alive_false_when_no_context() -> None:
    state = MagicMock()
    state.is_connected = MagicMock(return_value=True)
    state.browser_context = None

    assert await real_browser_manager._inherited_browser_transport_alive(state) is False


@pytest.mark.asyncio
async def test_inherited_browser_transport_alive_true_when_probe_succeeds() -> None:
    state = MagicMock()
    state.is_connected = MagicMock(return_value=True)
    state.browser_context = MagicMock()
    state.browser_context.cookies = AsyncMock(return_value=[])

    assert await real_browser_manager._inherited_browser_transport_alive(state) is True


def make_task(
    task_id: str,
    organization_id: str = "org_test",
    proxy_location: object = None,
    workflow_run_id: str | None = None,
) -> MagicMock:
    task = MagicMock()
    task.task_id = task_id
    task.organization_id = organization_id
    task.proxy_location = proxy_location
    task.workflow_run_id = workflow_run_id
    task.url = "https://example.com"
    task.workflow_permanent_id = None
    task.extra_http_headers = None
    task.browser_address = None
    return task


def make_session(proxy_location: object = None, proxy_session_id: str | None = None) -> MagicMock:
    session = MagicMock()
    session.proxy_location = proxy_location
    session.proxy_session_id = proxy_session_id
    return session


def _merge_cloud_proxy_session_headers(
    extra_http_headers: dict[str, str] | None,
    proxy_session_id: str,
) -> dict[str, str]:
    headers = dict(extra_http_headers or {})
    headers.setdefault("dedicated-ip", proxy_session_id)
    return headers


@pytest.mark.asyncio
async def test_task_browser_inherits_session_proxy_when_no_browser_state() -> None:
    """When a task has a browser_session_id and no in-memory browser state, the session's proxy_location is used."""
    manager = RealBrowserManager()
    task = make_task("tsk_1", proxy_location="RESIDENTIAL")
    new_browser_state = MagicMock()
    new_browser_state.get_or_create_page = AsyncMock()

    session_proxy = "RESIDENTIAL_DE"
    session = make_session(proxy_location=session_proxy)

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.AGENT_FUNCTION.merge_proxy_session_extra_http_headers.side_effect = _merge_cloud_proxy_session_headers
        mock_app.PERSISTENT_SESSIONS_MANAGER.begin_session = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=None)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=session)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        with patch.object(
            manager, "_create_browser_state", new=AsyncMock(return_value=new_browser_state)
        ) as mock_create:
            await manager.get_or_create_for_task(task=task, browser_session_id="pbs_123")

        mock_create.assert_awaited_once()
        _, kwargs = mock_create.call_args
        assert kwargs["proxy_location"] == session_proxy


@pytest.mark.asyncio
async def test_task_does_not_cache_a_browser_rejected_by_terminal_session() -> None:
    manager = RealBrowserManager()
    task = make_task("tsk_rejected")
    candidate = MagicMock()
    candidate.get_or_create_page = AsyncMock()

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.begin_session = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=None)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=None)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock(
            side_effect=BrowserStatePublicationRejected("pbs_rejected")
        )
        with patch.object(manager, "_create_browser_state", new=AsyncMock(return_value=candidate)):
            with pytest.raises(BrowserStatePublicationRejected):
                await manager.get_or_create_for_task(task=task, browser_session_id="pbs_rejected")

    assert task.task_id not in manager.pages
    candidate.get_or_create_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_browser_inherits_session_proxy_pin_when_no_browser_state() -> None:
    manager = RealBrowserManager()
    task = make_task("tsk_1", proxy_location="RESIDENTIAL")
    task.extra_http_headers = {"X-Test": "1"}
    new_browser_state = MagicMock()
    new_browser_state.get_or_create_page = AsyncMock()

    session = make_session(proxy_location="RESIDENTIAL_ISP", proxy_session_id="abc1234567")

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.AGENT_FUNCTION.merge_proxy_session_extra_http_headers.side_effect = _merge_cloud_proxy_session_headers
        mock_app.PERSISTENT_SESSIONS_MANAGER.begin_session = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=None)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=session)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        with patch.object(
            manager, "_create_browser_state", new=AsyncMock(return_value=new_browser_state)
        ) as mock_create:
            await manager.get_or_create_for_task(task=task, browser_session_id="pbs_123")

    expected_headers = {"X-Test": "1", "dedicated-ip": "abc1234567"}
    assert mock_create.await_args.kwargs["extra_http_headers"] == expected_headers
    assert new_browser_state.get_or_create_page.await_args.kwargs["extra_http_headers"] == expected_headers
    assert task.extra_http_headers == {"X-Test": "1"}


@pytest.mark.asyncio
async def test_task_browser_uses_task_proxy_when_session_has_no_proxy() -> None:
    """When the session has no proxy_location, the task's proxy_location is used."""
    manager = RealBrowserManager()
    task_proxy = "RESIDENTIAL_US"
    task = make_task("tsk_2", proxy_location=task_proxy)
    new_browser_state = MagicMock()
    new_browser_state.get_or_create_page = AsyncMock()

    session = make_session(proxy_location=None)

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.begin_session = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=None)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=session)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        with patch.object(
            manager, "_create_browser_state", new=AsyncMock(return_value=new_browser_state)
        ) as mock_create:
            await manager.get_or_create_for_task(task=task, browser_session_id="pbs_123")

        mock_create.assert_awaited_once()
        _, kwargs = mock_create.call_args
        assert kwargs["proxy_location"] == task_proxy


@pytest.mark.asyncio
async def test_workflow_run_browser_inherits_session_proxy_when_no_browser_state() -> None:
    """When a workflow run has a browser_session_id and no in-memory state, the session's proxy is used."""
    manager = RealBrowserManager()
    workflow_run = make_workflow_run("wfr_1")
    workflow_run.proxy_location = "RESIDENTIAL"

    new_browser_state = MagicMock()
    new_browser_state.get_or_create_page = AsyncMock()

    session_proxy = "RESIDENTIAL_FR"
    session = make_session(proxy_location=session_proxy)

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=None)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=session)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        with patch.object(
            manager, "_create_browser_state", new=AsyncMock(return_value=new_browser_state)
        ) as mock_create:
            await manager.get_or_create_for_workflow_run(
                workflow_run=workflow_run,
                url="https://example.com",
                browser_session_id="pbs_456",
            )

        mock_create.assert_awaited_once()
        _, kwargs = mock_create.call_args
        assert kwargs["proxy_location"] == session_proxy


@pytest.mark.asyncio
async def test_workflow_does_not_cache_a_browser_rejected_by_terminal_session() -> None:
    manager = RealBrowserManager()
    workflow_run = make_workflow_run("wfr_rejected")
    candidate = MagicMock()
    candidate.get_or_create_page = AsyncMock()

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=None)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=None)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock(
            side_effect=BrowserStatePublicationRejected("pbs_rejected")
        )
        with patch.object(manager, "_create_browser_state", new=AsyncMock(return_value=candidate)):
            with pytest.raises(BrowserStatePublicationRejected):
                await manager.get_or_create_for_workflow_run(
                    workflow_run=workflow_run,
                    url="https://example.com",
                    browser_session_id="pbs_rejected",
                )

    assert workflow_run.workflow_run_id not in manager.pages
    candidate.get_or_create_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_workflow_run_browser_inherits_session_proxy_pin_when_no_browser_state() -> None:
    manager = RealBrowserManager()
    workflow_run = make_workflow_run("wfr_1")
    workflow_run.extra_http_headers = {"X-Test": "1"}

    new_browser_state = MagicMock()
    new_browser_state.get_or_create_page = AsyncMock()

    session = make_session(proxy_location="RESIDENTIAL_ISP", proxy_session_id="abc1234567")

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.AGENT_FUNCTION.merge_proxy_session_extra_http_headers.side_effect = _merge_cloud_proxy_session_headers
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=None)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=session)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        with patch.object(
            manager, "_create_browser_state", new=AsyncMock(return_value=new_browser_state)
        ) as mock_create:
            await manager.get_or_create_for_workflow_run(
                workflow_run=workflow_run,
                url="https://example.com",
                browser_session_id="pbs_456",
            )

    expected_headers = {"X-Test": "1", "dedicated-ip": "abc1234567"}
    assert mock_create.await_args.kwargs["extra_http_headers"] == expected_headers
    assert new_browser_state.get_or_create_page.await_args.kwargs["extra_http_headers"] == expected_headers
    assert workflow_run.extra_http_headers == {"X-Test": "1"}


@pytest.mark.asyncio
async def test_workflow_run_browser_uses_workflow_proxy_when_session_has_no_proxy() -> None:
    """When the session has no proxy_location, the workflow run's proxy_location is used."""
    manager = RealBrowserManager()
    workflow_run = make_workflow_run("wfr_2")
    wf_proxy = "RESIDENTIAL_IE"
    workflow_run.proxy_location = wf_proxy

    new_browser_state = MagicMock()
    new_browser_state.get_or_create_page = AsyncMock()

    session = make_session(proxy_location=None)

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=None)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_session = AsyncMock(return_value=session)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        with patch.object(
            manager, "_create_browser_state", new=AsyncMock(return_value=new_browser_state)
        ) as mock_create:
            await manager.get_or_create_for_workflow_run(
                workflow_run=workflow_run,
                url="https://example.com",
                browser_session_id="pbs_456",
            )

        mock_create.assert_awaited_once()
        _, kwargs = mock_create.call_args
        assert kwargs["proxy_location"] == wf_proxy


def _make_browser_state_with_video(video_path: str) -> MagicMock:
    video_artifact = MagicMock()
    video_artifact.video_path = video_path
    video_artifact.video_data = None
    browser_state = MagicMock()
    browser_state.browser_artifacts.video_artifacts = [video_artifact]
    return browser_state


@pytest.mark.asyncio
async def test_get_video_artifacts_finalize_true_prepares_upload(tmp_path) -> None:
    """The default (finalize=True) path prepares finalized recording bytes and extension."""
    src = tmp_path / "recording.webm"
    src.write_bytes(b"raw-webm-bytes")
    prepared = tmp_path / "recording.mp4"
    prepared.write_bytes(b"compressed-mp4-bytes")
    browser_state = _make_browser_state_with_video(str(src))

    with patch(
        "skyvern.webeye.real_browser_manager.prepare_recording_for_upload",
        lambda path: fake_prepared_recording(path, str(prepared)),
    ):
        artifacts = await RealBrowserManager().get_video_artifacts(browser_state=browser_state)

    assert artifacts[0].video_data == b"compressed-mp4-bytes"
    assert artifacts[0].video_file_extension == "mp4"


@pytest.mark.asyncio
async def test_get_video_artifacts_finalize_false_skips_ffmpeg(tmp_path) -> None:
    """finalize=False is the per-step-snapshot path: read raw bytes, never spawn ffmpeg.

    This is what prevents long browser tasks from firing one ffmpeg subprocess per step
    (the step-sync runs while the recording file is still open — remux is pointless there).
    """
    src = tmp_path / "recording.webm"
    src.write_bytes(b"partial-webm-bytes")
    browser_state = _make_browser_state_with_video(str(src))

    with patch("skyvern.webeye.real_browser_manager.prepare_recording_for_upload") as m:
        artifacts = await RealBrowserManager().get_video_artifacts(browser_state=browser_state, finalize=False)

    m.assert_not_called()
    assert artifacts[0].video_data == b"partial-webm-bytes"
    assert artifacts[0].video_file_extension == "webm"


@pytest.mark.asyncio
async def test_get_video_artifacts_non_webm_skips_ffmpeg(tmp_path) -> None:
    """Non-WebM container files (e.g. fully-formed MP4 from a remote source)
    are container-valid already; the extension-based short-circuit reads them raw."""
    src = tmp_path / "recording.mp4"
    src.write_bytes(b"mp4-bytes")
    browser_state = _make_browser_state_with_video(str(src))

    with patch.object(real_browser_manager, "prepare_recording_for_upload", new=AsyncMock()) as m:
        artifacts = await RealBrowserManager().get_video_artifacts(browser_state=browser_state)

    m.assert_not_called()
    assert artifacts[0].video_data == b"mp4-bytes"


@pytest.mark.asyncio
async def test_get_video_artifacts_empty_snapshot_path_does_not_warn() -> None:
    """The finalize=False snapshot path runs once per step on browsers that never record
    locally (remote/CDP/persistent sessions), where an empty list is the expected state."""
    browser_state = MagicMock()
    browser_state.browser_artifacts.video_artifacts = []

    with patch.object(real_browser_manager, "LOG") as mock_log:
        artifacts = await RealBrowserManager().get_video_artifacts(browser_state=browser_state, finalize=False)

    assert artifacts == []
    mock_log.warning.assert_not_called()


@pytest.mark.asyncio
async def test_get_video_artifacts_empty_finalize_path_warns() -> None:
    """At finalize time the browser is closing, so a missing recording is a real signal."""
    browser_state = MagicMock()
    browser_state.browser_artifacts.video_artifacts = []

    with patch.object(real_browser_manager, "LOG") as mock_log:
        artifacts = await RealBrowserManager().get_video_artifacts(browser_state=browser_state)

    assert artifacts == []
    mock_log.warning.assert_called_once()


def _make_page_mock(video_path: str | None) -> MagicMock:
    page = MagicMock()
    if video_path is None:
        page.video = None
    else:
        page.video = MagicMock()
        page.video.path = AsyncMock(return_value=video_path)
    return page


@pytest.mark.asyncio
async def test_popup_video_listener_picks_up_popup_page() -> None:
    """set_popup_video_listener registers popup video paths on the page event."""

    artifacts = BrowserArtifacts(video_artifacts=[VideoArtifact(video_path="/tmp/videos/main.webm")])
    browser_context = MagicMock()
    set_popup_video_listener(browser_context=browser_context, browser_artifacts=artifacts)

    handler = browser_context.on.call_args[0][1]
    popup = _make_page_mock("/tmp/videos/popup.webm")
    await handler(popup)

    paths = [va.video_path for va in artifacts.video_artifacts]
    assert paths == ["/tmp/videos/main.webm", "/tmp/videos/popup.webm"]


@pytest.mark.asyncio
async def test_popup_video_listener_deduplicates() -> None:
    """Already-tracked pages are not added twice."""

    artifacts = BrowserArtifacts(video_artifacts=[VideoArtifact(video_path="/tmp/videos/main.webm")])
    browser_context = MagicMock()
    set_popup_video_listener(browser_context=browser_context, browser_artifacts=artifacts)

    handler = browser_context.on.call_args[0][1]
    page = _make_page_mock("/tmp/videos/main.webm")
    await handler(page)

    assert len(artifacts.video_artifacts) == 1


@pytest.mark.asyncio
async def test_popup_video_listener_skips_pages_without_video() -> None:
    """Pages with no video (e.g. about:blank) are silently skipped."""

    artifacts = BrowserArtifacts()
    browser_context = MagicMock()
    set_popup_video_listener(browser_context=browser_context, browser_artifacts=artifacts)

    handler = browser_context.on.call_args[0][1]
    await handler(_make_page_mock(None))

    assert len(artifacts.video_artifacts) == 0


@pytest.mark.asyncio
async def test_popup_video_listener_multiple_popups() -> None:
    """Multiple popup pages from loop iterations are all captured."""

    artifacts = BrowserArtifacts(video_artifacts=[VideoArtifact(video_path="/tmp/videos/main.webm")])
    browser_context = MagicMock()
    set_popup_video_listener(browser_context=browser_context, browser_artifacts=artifacts)

    handler = browser_context.on.call_args[0][1]
    for name in ["popup1", "popup2", "popup3"]:
        await handler(_make_page_mock(f"/tmp/videos/{name}.webm"))

    paths = [va.video_path for va in artifacts.video_artifacts]
    assert paths == [
        "/tmp/videos/main.webm",
        "/tmp/videos/popup1.webm",
        "/tmp/videos/popup2.webm",
        "/tmp/videos/popup3.webm",
    ]


@pytest.mark.asyncio
async def test_popup_video_listener_skips_page_discarded_before_it_registers() -> None:
    """RealBrowserState.discard_page_video() may tombstone a page while
    set_popup_video_listener's fire-and-forget _on_page for that same page is still awaiting
    video.path() — the late registration must not re-append after the discard."""

    artifacts = BrowserArtifacts(video_artifacts=[VideoArtifact(video_path="/tmp/videos/main.webm")])
    browser_context = MagicMock()
    set_popup_video_listener(browser_context=browser_context, browser_artifacts=artifacts)

    handler = browser_context.on.call_args[0][1]
    orphan = _make_page_mock("/tmp/videos/orphan.webm")

    # Simulate the discard landing first (RealBrowserState._close_all_other_pages tombstones
    # synchronously, before it ever awaits anything), then the listener's registration resolving.
    artifacts.discard_page_video(orphan)
    await handler(orphan)

    paths = [va.video_path for va in artifacts.video_artifacts]
    assert paths == ["/tmp/videos/main.webm"]


@pytest.mark.asyncio
async def test_set_working_page_does_not_touch_video_artifacts() -> None:
    """set_working_page only sets the working page; video tracking is handled by the listener."""

    artifacts = BrowserArtifacts()
    state = RealBrowserState(pw=MagicMock(), browser_context=MagicMock(), browser_artifacts=artifacts)

    page = _make_page_mock("/tmp/v/page.webm")
    await state.set_working_page(page, index=0)

    assert len(artifacts.video_artifacts) == 0


@pytest.mark.asyncio
async def test_popup_video_listener_registers_pre_existing_pages() -> None:
    """Pages that already exist when the listener is registered are captured."""
    import asyncio

    artifacts = BrowserArtifacts()
    initial_page = _make_page_mock("/tmp/videos/initial.webm")
    browser_context = MagicMock()
    browser_context.pages = [initial_page]
    set_popup_video_listener(browser_context=browser_context, browser_artifacts=artifacts)

    # Let the ensure_future tasks run to completion (registration spans multiple loop turns)
    async with asyncio.timeout(1):
        while not artifacts.video_artifacts:
            await asyncio.sleep(0)

    paths = [va.video_path for va in artifacts.video_artifacts]
    assert paths == ["/tmp/videos/initial.webm"]


@pytest.mark.asyncio
async def test_popup_video_listener_page_closed_no_warning() -> None:
    """PlaywrightError (e.g. Page closed) must not produce a WARNING log."""
    import structlog.testing
    from playwright.async_api import Error as PlaywrightError

    artifacts = BrowserArtifacts()
    browser_context = MagicMock()
    set_popup_video_listener(browser_context=browser_context, browser_artifacts=artifacts)

    handler = browser_context.on.call_args[0][1]
    page = MagicMock()
    page.video = MagicMock()
    page.video.path = AsyncMock(side_effect=PlaywrightError("Page closed"))

    with structlog.testing.capture_logs() as cap:
        await handler(page)

    assert len(artifacts.video_artifacts) == 0
    warning_events = [e for e in cap if e["log_level"] == "warning"]
    assert len(warning_events) == 0


@pytest.mark.asyncio
async def test_popup_video_listener_timeout_logs_sanitized_origin() -> None:
    """TimeoutError logs WARNING with only the domain, no query params or PII."""
    import structlog.testing

    artifacts = BrowserArtifacts()
    browser_context = MagicMock()
    set_popup_video_listener(browser_context=browser_context, browser_artifacts=artifacts)

    handler = browser_context.on.call_args[0][1]
    page = MagicMock()
    page.video = MagicMock()
    page.video.path = AsyncMock(side_effect=TimeoutError())
    page.url = "https://user:pass@example.com/o/oauth2/auth?client_id=secret&redirect_uri=https://evil.com"

    with structlog.testing.capture_logs() as cap:
        await handler(page)

    assert len(artifacts.video_artifacts) == 0
    warning_events = [e for e in cap if e["log_level"] == "warning"]
    assert len(warning_events) == 1
    logged = str(warning_events[0])
    assert "example.com" in logged  # nosemgrep: incomplete-url-substring-sanitization
    assert "user:pass" not in logged
    assert "client_id=secret" not in logged
    assert "redirect_uri" not in logged


@pytest.mark.asyncio
async def test_popup_video_listener_timeout_url_error_safe() -> None:
    """If page.url itself raises, the handler still completes without crashing."""
    artifacts = BrowserArtifacts()
    browser_context = MagicMock()
    set_popup_video_listener(browser_context=browser_context, browser_artifacts=artifacts)

    handler = browser_context.on.call_args[0][1]
    page = MagicMock()
    page.video = MagicMock()
    page.video.path = AsyncMock(side_effect=TimeoutError())
    type(page).url = property(lambda self: (_ for _ in ()).throw(RuntimeError("page destroyed")))

    await handler(page)
    assert len(artifacts.video_artifacts) == 0


@pytest.mark.parametrize(
    ("shared_with_parent", "expected_deferred_close", "expected_release_driver"),
    [(False, True, None), (True, False, False)],
)
@pytest.mark.asyncio
async def test_cleanup_persists_session_cookies_when_close_deferred_for_streams(
    monkeypatch: pytest.MonkeyPatch,
    shared_with_parent: bool,
    expected_deferred_close: bool,
    expected_release_driver: bool | None,
) -> None:
    """Active CDP streams defer the browser close, so cleanup must snapshot session cookies before
    store_browser_session archives the dir — the deferred close runs too late."""
    manager = RealBrowserManager()
    browser_state = MagicMock()
    browser_state.browser_artifacts.traces_dir = None
    browser_state.browser_artifacts.browser_session_dir = "/tmp/fake_profile"
    browser_state.close = AsyncMock()
    manager.pages["wfr_streamed"] = browser_state
    manager.pages["tsk_streamed"] = browser_state
    if shared_with_parent:
        manager.pages["wr_parent"] = browser_state

    persist_mock = AsyncMock()
    defer_mock = MagicMock(return_value=True)
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.persist_session_cookies", persist_mock)
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.stream_ref_active", lambda wrid: True)
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.set_deferred_close_params", defer_mock)

    # The parent alias only suppresses the close when it is a genuinely live run in this process.
    with _live_workflow_run_contexts("wr_parent"):
        result = await manager.cleanup_for_workflow_run(
            "wfr_streamed",
            task_ids=["tsk_streamed"],
            close_browser_on_completion=True,
        )

    persist_mock.assert_awaited_once_with(browser_state.browser_context, "/tmp/fake_profile")
    defer_mock.assert_called_once_with(
        "wfr_streamed",
        expected_deferred_close,
        release_driver=expected_release_driver,
    )
    browser_state.close.assert_not_awaited()
    assert "tsk_streamed" not in manager.pages
    assert result.recording_finalized is False


@pytest.mark.parametrize("close_succeeded", [True, False])
@pytest.mark.asyncio
async def test_cleanup_closes_when_stream_disconnects_before_deferral(
    monkeypatch: pytest.MonkeyPatch,
    close_succeeded: bool,
) -> None:
    manager = RealBrowserManager()
    browser_state = MagicMock()
    browser_state.browser_artifacts.traces_dir = None
    browser_state.browser_artifacts.browser_session_dir = "/tmp/fake_profile"
    browser_state.close = AsyncMock(return_value=close_succeeded)
    manager.pages["wfr_streamed"] = browser_state
    manager.pages["tsk_streamed"] = browser_state

    monkeypatch.setattr(
        "skyvern.webeye.real_browser_manager.persist_session_cookies",
        AsyncMock(),
    )
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.stream_ref_active", lambda wrid: True)
    monkeypatch.setattr(
        "skyvern.webeye.real_browser_manager.set_deferred_close_params",
        lambda *args, **kwargs: False,
    )

    result = await manager.cleanup_for_workflow_run(
        "wfr_streamed",
        task_ids=["tsk_streamed"],
        close_browser_on_completion=True,
    )

    browser_state.close.assert_awaited_once_with(close_browser_on_completion=True, release_driver=None)
    assert "tsk_streamed" not in manager.pages
    assert result.recording_finalized is close_succeeded


@pytest.mark.asyncio
async def test_public_workflow_cleanup_defers_owner_release_until_final_stream_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run_id = "wfr_owner_stream"
    manager = RealBrowserManager()
    browser_state = MagicMock()
    browser_state.browser_artifacts.traces_dir = None
    browser_state.browser_artifacts.browser_session_dir = "/tmp/fake_profile"
    browser_state.close = AsyncMock(return_value=False)
    manager.pages[workflow_run_id] = browser_state
    manager._persistent_session_leases[workflow_run_id] = _PersistentSessionLease(
        session_id="pbs_owner_stream",
        organization_id="org_test",
        runnable_id=workflow_run_id,
        browser_state=browser_state,
    )
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock(return_value=True)
    fake_app = MagicMock(BROWSER_MANAGER=manager, PERSISTENT_SESSIONS_MANAGER=sessions)

    import skyvern.forge as forge_module

    monkeypatch.setattr(forge_module, "app", fake_app)
    monkeypatch.setattr(real_browser_manager, "app", fake_app)
    monkeypatch.setattr(real_browser_manager, "persist_session_cookies", AsyncMock())
    assert registries.try_stream_ref_inc(workflow_run_id) is True

    await manager.cleanup_for_workflow_run(
        workflow_run_id,
        task_ids=[],
        close_browser_on_completion=False,
        browser_session_id="pbs_owner_stream",
        organization_id="org_test",
    )

    sessions.release_browser_session.assert_not_awaited()
    browser_state.close.assert_not_awaited()

    await registries.stream_ref_dec(workflow_run_id)

    browser_state.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=False)
    sessions.release_browser_session.assert_awaited_once_with(
        session_id="pbs_owner_stream",
        organization_id="org_test",
        expected_runnable_id=workflow_run_id,
        expected_runnable_generation_id=None,
        expected_browser_state=browser_state,
    )


@pytest.mark.asyncio
async def test_public_workflow_cleanup_installs_closing_tombstone_before_first_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run_id = "wfr_cleanup_race"
    manager = RealBrowserManager()
    cleanup_awaited = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def blocked_drop(_: str) -> None:
        cleanup_awaited.set()
        await allow_cleanup.wait()

    monkeypatch.setattr(manager, "_drop_engine_owner", blocked_drop)
    cleanup = asyncio.create_task(manager.cleanup_for_workflow_run(workflow_run_id, task_ids=[]))
    await cleanup_awaited.wait()

    attached = registries.try_stream_ref_inc(workflow_run_id)
    allow_cleanup.set()
    await cleanup
    assert attached is False


@pytest.mark.asyncio
async def test_script_acquisition_reports_a_live_session_before_the_lease_exists() -> None:
    # begin_session publishes occupancy, but the lease only lands once the attach returns — and occupy
    # does not extend started_at/timeout_minutes. A reused session already past timeout+grace would
    # therefore sit occupied-but-unleased for the whole attach, and the reaper (which reads liveness
    # from this manager) would reap it out from under the run.
    manager = RealBrowserManager()
    pbs_state = MagicMock()
    pbs_state.get_working_page = AsyncMock(return_value=None)
    pbs_state.get_or_create_page = AsyncMock()
    during_attach: list[set[str]] = []

    async def _begin_session(**kwargs: object) -> str:
        during_attach.append(manager.live_session_runnable_ids())
        return "gen_attach"

    async def _get_browser_state(*args: object, **kwargs: object) -> MagicMock:
        during_attach.append(manager.live_session_runnable_ids())
        return pbs_state

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.begin_session = AsyncMock(side_effect=_begin_session)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(side_effect=_get_browser_state)

        await manager.get_or_create_for_script(
            script_id="s_attach",
            browser_session_id="pbs_attach",
            organization_id="org_test",
        )

    assert during_attach == [{"s_attach"}, {"s_attach"}]
    # The lease takes over with no gap once the attach completes.
    assert manager._persistent_session_leases["s_attach"].runnable_id == "s_attach"
    assert manager.live_session_runnable_ids() == {"s_attach"}


@pytest.mark.asyncio
async def test_attached_run_renews_the_session_activity_lease() -> None:
    # Only the CDP proxy wrote last_activity_at, so a run attached off-proxy read as never-active and
    # its session was closed mid-step (SKY-15568).
    manager = RealBrowserManager()
    pbs_state = MagicMock()
    pbs_state.get_working_page = AsyncMock(return_value=None)
    pbs_state.get_or_create_page = AsyncMock()

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch.object(real_browser_manager, "SESSION_ACTIVITY_RENEWAL_INTERVAL_SECONDS", 0.01),
    ):
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.begin_session = AsyncMock(return_value="gen_renew")
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=pbs_state)
        touch = AsyncMock()
        mock_app.DATABASE.browser_sessions.touch_last_activity = touch

        try:
            await manager.get_or_create_for_script(
                script_id="s_renew",
                browser_session_id="pbs_renew",
                organization_id="org_test",
            )

            await asyncio.sleep(0.05)
            assert touch.await_args_list.count(call("pbs_renew")) > 0

            # A failed release keeps the lease for cleanup attribution; renewal must still stop there.
            mock_app.PERSISTENT_SESSIONS_MANAGER.release_browser_session = AsyncMock(return_value=False)
            await manager._release_persistent_session(
                "pbs_renew", "org_test", manager._persistent_session_leases["s_renew"]
            )
            assert "s_renew" in manager._persistent_session_leases, "the lease is retained on a failed release"

            touch.reset_mock()
            await asyncio.sleep(0.05)
            assert touch.await_args_list == []

            # A successful release drops the lease and its marker, and the renewer exits with nothing left.
            pbs_state.close = AsyncMock()
            pbs_state.browser_artifacts.traces_dir = None
            mock_app.PERSISTENT_SESSIONS_MANAGER.release_browser_session = AsyncMock(return_value=True)
            await manager.cleanup_for_script("s_renew", browser_session_id="pbs_renew", organization_id="org_test")
            assert "s_renew" not in manager._persistent_session_leases
            assert manager._released_session_ids == set()
            await asyncio.sleep(0.05)
            assert manager._session_activity_renewer is not None and manager._session_activity_renewer.done()
        finally:
            if manager._session_activity_renewer is not None:
                manager._session_activity_renewer.cancel()


@pytest.mark.asyncio
async def test_failed_script_acquisition_leaves_no_live_session_behind() -> None:
    # A cold/evicted session fails closed. The in-flight marker must not outlive the attempt, or a run
    # that never started would protect the session from the reaper forever.
    manager = RealBrowserManager()

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.begin_session = AsyncMock(return_value="gen_cold")
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=None)

        with pytest.raises(MissingBrowserStateForBrowserSession):
            await manager.get_or_create_for_script(
                script_id="s_cold",
                browser_session_id="pbs_cold",
                organization_id="org_test",
            )

    assert manager.live_session_runnable_ids() == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "release_outcome",
    [{"return_value": False}, {"side_effect": RuntimeError("db unreachable")}],
    ids=["cas_miss", "raises"],
)
async def test_script_cleanup_drops_lease_even_when_release_fails(
    monkeypatch: pytest.MonkeyPatch,
    release_outcome: dict,
) -> None:
    # Unlike a workflow run, a script's cleanup runs once at run_script's terminal boundary, so
    # nothing re-invokes it to retry a failed release — retaining the lease can only strand it. The
    # reaper reads that lease as "still running" and would skip the session forever, so drop the
    # lease either way and leave the still-occupied row to the reaper.
    script_id = "s_release_failed"
    manager = RealBrowserManager()
    browser_state = MagicMock()
    browser_state.browser_artifacts.traces_dir = None
    browser_state.close = AsyncMock(return_value=False)
    manager.pages[script_id] = browser_state
    manager._persistent_session_leases[script_id] = _PersistentSessionLease(
        session_id="pbs_release_failed",
        organization_id="org_test",
        runnable_id=script_id,
        browser_state=browser_state,
    )
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock(**release_outcome)
    monkeypatch.setattr(real_browser_manager, "app", MagicMock(PERSISTENT_SESSIONS_MANAGER=sessions))

    await manager.cleanup_for_script(
        script_id,
        close_browser_on_completion=False,
        browser_session_id="pbs_release_failed",
        organization_id="org_test",
    )

    assert script_id not in manager._persistent_session_leases
    sessions.release_browser_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_workflow_cleanup_retains_owner_lease_until_release_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run_id = "wfr_release_retry"
    manager = RealBrowserManager()
    browser_state = MagicMock()
    browser_state.browser_artifacts.traces_dir = None
    browser_state.close = AsyncMock(return_value=False)
    manager.pages[workflow_run_id] = browser_state
    lease = _PersistentSessionLease(
        session_id="pbs_release_retry",
        organization_id="org_test",
        runnable_id=workflow_run_id,
        browser_state=browser_state,
    )
    manager._persistent_session_leases[workflow_run_id] = lease
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock(side_effect=[False, True])
    fake_app = MagicMock(PERSISTENT_SESSIONS_MANAGER=sessions)
    monkeypatch.setattr(real_browser_manager, "app", fake_app)

    await manager.cleanup_for_workflow_run(
        workflow_run_id,
        task_ids=[],
        close_browser_on_completion=False,
        browser_session_id="pbs_release_retry",
        organization_id="org_test",
    )

    assert manager._persistent_session_leases[workflow_run_id] is lease
    assert registries.try_stream_ref_inc(workflow_run_id) is False

    await manager.cleanup_for_workflow_run(
        workflow_run_id,
        task_ids=[],
        close_browser_on_completion=False,
        browser_session_id="pbs_release_retry",
        organization_id="org_test",
    )

    assert workflow_run_id not in manager._persistent_session_leases
    assert sessions.release_browser_session.await_count == 2
    browser_state.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=False)


@pytest.mark.asyncio
async def test_pbs_cleanup_is_release_only_not_terminal_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PBS lifetime is distributed — owned by runnable_id + generation CAS across Pods, not by this
    process. Even when the local non-PBS sharing predicate sees no other alias (``shared=False``,
    i.e. "this process could close it"), a cleanup carrying a browser_session_id must NOT terminal-
    close the remote browser: it detaches the local driver (``release_driver=False``) and releases
    occupancy only through the expected-owner CAS (PBS/non-PBS boundary)."""
    workflow_run_id = "wr_pbs_owner"
    manager = RealBrowserManager()
    browser_state = MagicMock()
    browser_state.browser_artifacts.traces_dir = None
    browser_state.close = AsyncMock(return_value=False)
    manager.pages[workflow_run_id] = browser_state
    lease = _PersistentSessionLease(
        session_id="pbs_owner",
        organization_id="org_test",
        runnable_id=workflow_run_id,
        browser_state=browser_state,
    )
    manager._persistent_session_leases[workflow_run_id] = lease
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock(return_value=True)
    monkeypatch.setattr(real_browser_manager, "app", MagicMock(PERSISTENT_SESSIONS_MANAGER=sessions))

    await manager.cleanup_for_workflow_run(
        workflow_run_id,
        task_ids=[],
        close_browser_on_completion=False,
        browser_session_id="pbs_owner",
        organization_id="org_test",
    )

    browser_state.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=False)
    sessions.release_browser_session.assert_awaited_once_with(
        session_id="pbs_owner",
        organization_id="org_test",
        expected_runnable_id=workflow_run_id,
        expected_runnable_generation_id=None,
        expected_browser_state=browser_state,
    )
    assert workflow_run_id not in manager._persistent_session_leases


@pytest.mark.asyncio
async def test_pbs_adoption_rebinds_download_dir_to_run_id() -> None:
    """Adopting a persistent session must rebind its CDP download dir to the run's id (SKY-11083)."""
    manager = RealBrowserManager()
    workflow_run = make_workflow_run("wfr_adopt")

    adopted_browser = MagicMock()
    pbs_state = MagicMock()
    pbs_state.browser_context.browser = adopted_browser
    pbs_state.get_working_page = AsyncMock(return_value=None)
    pbs_state.get_or_create_page = AsyncMock()

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch("skyvern.webeye.real_browser_manager.rebind_download_dir", new_callable=AsyncMock) as mock_rebind,
    ):
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.begin_session = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=pbs_state)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url=None,
            browser_session_id="bs_adopt",
        )
        mock_rebind.assert_awaited_once_with(adopted_browser, run_id="wfr_adopt")
        mock_rebind.reset_mock()
        await real_browser_manager._rebind_pbs_download_dir(pbs_state, workflow_run.workflow_run_id, "bs_adopt")
        mock_rebind.assert_not_awaited()


@pytest.mark.asyncio
async def test_pbs_adoption_rebinds_download_dir_without_an_interceptor() -> None:
    """A Skyvern-hosted session binds no interceptor, so it is rebind_download_dir's
    Browser.setDownloadBehavior branch that moves it off the session-scoped connect-time path.
    SimpleNamespace, not MagicMock: the latter auto-creates the interceptor attribute."""
    adopted_browser = MagicMock()
    browser_context = SimpleNamespace(browser=adopted_browser)
    # A real BrowserState always carries browser_artifacts; default RUN_DIR keeps today's rebind path.
    pbs_state = SimpleNamespace(browser_context=browser_context, browser_artifacts=BrowserArtifacts())

    with patch("skyvern.webeye.real_browser_manager.rebind_download_dir", new_callable=AsyncMock) as mock_rebind:
        await real_browser_manager._rebind_pbs_download_dir(pbs_state, "wfr_own_infra", "bs_own_infra")
        mock_rebind.assert_awaited_once_with(adopted_browser, run_id="wfr_own_infra")

        mock_rebind.reset_mock()
        await real_browser_manager._rebind_pbs_download_dir(pbs_state, "wfr_own_infra", "bs_own_infra")
        mock_rebind.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_workflow_adoption_keeps_lease_identity_separate_from_download_run_id() -> None:
    manager = RealBrowserManager()
    workflow_run = make_workflow_run("wr_owner")
    adopted_browser = MagicMock()
    pbs_state = MagicMock()
    pbs_state.browser_context.browser = adopted_browser
    pbs_state.browser_context._skyvern_cdp_download_interceptor = MagicMock()
    pbs_state.get_working_page = AsyncMock(return_value=None)
    pbs_state.get_or_create_page = AsyncMock()

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch("skyvern.webeye.real_browser_manager.rebind_download_dir", new_callable=AsyncMock) as mock_rebind,
        skyvern_context.scoped(
            SkyvernContext(
                organization_id="org_test",
                workflow_run_id="wr_owner",
                run_id="task_v2_run",
            )
        ),
    ):
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=pbs_state)

        await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            browser_session_id="pbs_nested",
            navigate=False,
        )

    mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.assert_awaited_once_with(
        "pbs_nested",
        organization_id="org_test",
        expected_runnable_id="wr_owner",
        download_run_id="task_v2_run",
    )
    mock_rebind.assert_awaited_once_with(adopted_browser, run_id="task_v2_run")
    assert manager._persistent_session_leases["wr_owner"].runnable_id == "wr_owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_remote_interceptor", [True, False])
async def test_pbs_task_adoption_rebinds_regardless_of_remote_interceptor(has_remote_interceptor: bool) -> None:
    """Own-infra sessions carry no interceptor and are rebound through setDownloadBehavior; skipping
    them would leave their downloads in the session-scoped connect-time dir, which collection
    (get_download_dir(run_id)) never reads."""
    manager = RealBrowserManager()
    task = make_task("tsk_adopt")
    adopted_browser = MagicMock()
    pbs_state = MagicMock()
    pbs_state.browser_context.browser = adopted_browser
    pbs_state.browser_context._skyvern_cdp_download_interceptor = MagicMock() if has_remote_interceptor else None
    pbs_state.get_working_page = AsyncMock(return_value=None)
    pbs_state.get_or_create_page = AsyncMock()

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch(
            "skyvern.webeye.real_browser_manager._rebind_pbs_download_dir",
            new_callable=AsyncMock,
        ) as mock_rebind,
    ):
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.begin_session = AsyncMock(return_value=None)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=pbs_state)
        await manager.get_or_create_for_task(task, browser_session_id="bs_adopt")

    mock_rebind.assert_awaited_once_with(pbs_state, "tsk_adopt", "bs_adopt")
    mock_app.PERSISTENT_SESSIONS_MANAGER.begin_session.assert_awaited_once_with(
        browser_session_id="bs_adopt",
        runnable_type="task",
        runnable_id="tsk_adopt",
        organization_id="org_test",
    )
    mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.assert_awaited_once_with(
        "bs_adopt",
        organization_id="org_test",
        expected_runnable_id="tsk_adopt",
        download_run_id="tsk_adopt",
    )


@pytest.mark.asyncio
async def test_workflow_task_inherits_workflow_session_lease_without_beginning_task_lease() -> None:
    manager = RealBrowserManager()
    task = make_task("tsk_child", workflow_run_id="wr_owner")
    pbs_state = MagicMock()
    pbs_state.browser_context._skyvern_cdp_download_interceptor = None
    pbs_state.get_working_page = AsyncMock(return_value=None)
    pbs_state.get_or_create_page = AsyncMock()

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.begin_session = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=pbs_state)

        await manager.get_or_create_for_task(task, browser_session_id="bs_workflow")

    mock_app.PERSISTENT_SESSIONS_MANAGER.begin_session.assert_not_awaited()
    mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.assert_awaited_once_with(
        "bs_workflow",
        organization_id="org_test",
        expected_runnable_id="wr_owner",
        download_run_id="wr_owner",
    )
    lease = manager._persistent_session_leases["wr_owner"]
    assert lease.runnable_id == "wr_owner"
    assert lease.browser_state is pbs_state


@pytest.mark.asyncio
async def test_pbs_adoption_skips_rebind_when_no_browser() -> None:
    """Rebind must no-op when the adopted context exposes no owning browser (e.g. launch_persistent_context)."""
    manager = RealBrowserManager()
    workflow_run = make_workflow_run("wfr_no_browser")

    pbs_state = MagicMock()
    pbs_state.browser_context.browser = None
    pbs_state.get_working_page = AsyncMock(return_value=None)
    pbs_state.get_or_create_page = AsyncMock()

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch("skyvern.webeye.real_browser_manager.rebind_download_dir", new_callable=AsyncMock) as mock_rebind,
    ):
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=pbs_state)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url=None,
            browser_session_id="bs_no_browser",
        )

    mock_rebind.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_pbs_workflow_run_does_not_rebind() -> None:
    """The own-browser (no browser_session_id) path must run zero new download-rebind code (SKY-11083 regression guard)."""
    manager = RealBrowserManager()
    parent_state = MagicMock()
    parent_state.get_working_page = AsyncMock(return_value=MagicMock())
    manager.pages["wfr_parent"] = parent_state

    workflow_run = make_workflow_run("wfr_child", parent_workflow_run_id="wfr_parent")

    with patch("skyvern.webeye.real_browser_manager.rebind_download_dir", new_callable=AsyncMock) as mock_rebind:
        result = await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url=None,
            browser_session_id=None,
        )

    assert result is parent_state
    mock_rebind.assert_not_awaited()


@pytest.mark.asyncio
async def test_pbs_adoption_skips_rebind_when_session_dir_binding() -> None:
    """A provider-owned remote binding must preserve the provider-selected destination: the run-dir
    rebind must be skipped."""
    manager = RealBrowserManager()
    workflow_run = make_workflow_run("wfr_session")

    adopted_browser = MagicMock()
    pbs_state = MagicMock()
    pbs_state.browser_context.browser = adopted_browser
    pbs_state.browser_artifacts.download_binding = DownloadBinding.SESSION_DIR
    pbs_state.get_working_page = AsyncMock(return_value=None)
    pbs_state.get_or_create_page = AsyncMock()

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch("skyvern.webeye.real_browser_manager.rebind_download_dir", new_callable=AsyncMock) as mock_rebind,
    ):
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=pbs_state)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url=None,
            browser_session_id="bs_session",
        )

    mock_rebind.assert_not_awaited()


@pytest.mark.asyncio
async def test_pbs_adoption_rebinds_when_run_dir_binding() -> None:
    """A RUN_DIR (local/OSS) adopted session keeps today's rebind: it is the only delivery path there."""
    manager = RealBrowserManager()
    workflow_run = make_workflow_run("wfr_run_dir")

    adopted_browser = MagicMock()
    pbs_state = MagicMock()
    pbs_state.browser_context.browser = adopted_browser
    pbs_state.browser_artifacts.download_binding = DownloadBinding.RUN_DIR
    pbs_state.get_working_page = AsyncMock(return_value=None)
    pbs_state.get_or_create_page = AsyncMock()

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch("skyvern.webeye.real_browser_manager.rebind_download_dir", new_callable=AsyncMock) as mock_rebind,
    ):
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=pbs_state)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url=None,
            browser_session_id="bs_run_dir",
        )

    mock_rebind.assert_awaited_once_with(adopted_browser, run_id="wfr_run_dir")


def _stale_pbs_browser_state(*, navigate_exc: Exception) -> MagicMock:
    state = MagicMock()
    page = MagicMock()
    state.get_working_page = AsyncMock(return_value=page)
    state.navigate_to_url = AsyncMock(side_effect=navigate_exc)
    state.browser_context = MagicMock()
    state.browser_context.browser = MagicMock()
    return state


def _fresh_pbs_browser_state() -> MagicMock:
    state = MagicMock()
    page = MagicMock()
    state.get_working_page = AsyncMock(return_value=page)
    state.navigate_to_url = AsyncMock()
    state.get_or_create_page = AsyncMock()
    state.browser_context = MagicMock()
    state.browser_context.browser = MagicMock()
    return state


@pytest.mark.asyncio
async def test_pbs_navigate_evicts_and_retries_on_connection_closed_driver_error() -> None:
    """When the cached PBS BrowserState's first ``Page.goto`` raises
    ``FailedToNavigateToUrl`` with ``Connection closed while reading from the driver``,
    the manager must evict the cached entry, re-fetch a fresh BrowserState from
    ``PERSISTENT_SESSIONS_MANAGER``, and retry navigation once before surfacing the
    failure to the workflow run."""
    from skyvern.exceptions import FailedToNavigateToUrl

    manager = RealBrowserManager()
    stale = _stale_pbs_browser_state(
        navigate_exc=FailedToNavigateToUrl(
            url="https://example.com",
            error_message="Page.goto: Connection closed while reading from the driver",
        )
    )
    fresh = _fresh_pbs_browser_state()
    workflow_run = make_workflow_run("wfr_pbs")

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(side_effect=[stale, fresh])
        mock_app.PERSISTENT_SESSIONS_MANAGER.evict_cached_browser_state = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        result = await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id="pbs_abc",
        )

    assert result is fresh
    mock_app.PERSISTENT_SESSIONS_MANAGER.evict_cached_browser_state.assert_awaited_once_with(
        "pbs_abc",
        organization_id=workflow_run.organization_id,
        expected=stale,
    )
    assert mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.await_count == 2
    fresh.navigate_to_url.assert_awaited_once()


@pytest.mark.asyncio
async def test_pbs_navigate_does_not_retry_on_unrelated_error() -> None:
    """The evict-and-reconnect path is scoped to the cached-dead-CDP signal. A generic
    navigation failure (e.g. DNS error) must still bubble up so callers can route the
    real failure without an additional evict+reconnect cycle."""
    from skyvern.exceptions import FailedToNavigateToUrl

    manager = RealBrowserManager()
    stale = _stale_pbs_browser_state(
        navigate_exc=FailedToNavigateToUrl(
            url="https://example.com",
            error_message="net::ERR_NAME_NOT_RESOLVED",
        )
    )
    workflow_run = make_workflow_run("wfr_pbs")

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=stale)
        mock_app.PERSISTENT_SESSIONS_MANAGER.evict_cached_browser_state = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        with pytest.raises(FailedToNavigateToUrl):
            await manager.get_or_create_for_workflow_run(
                workflow_run=workflow_run,
                url="https://example.com",
                browser_session_id="pbs_abc",
            )

    mock_app.PERSISTENT_SESSIONS_MANAGER.evict_cached_browser_state.assert_not_awaited()
    assert mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.await_count == 1


@pytest.mark.asyncio
async def test_pbs_navigate_does_not_evict_on_page_only_close() -> None:
    """``Target page, context or browser has been closed`` is overloaded — Playwright
    surfaces it for page-only or context-only closes too, not just a dead CDP transport.
    The recovery path must NOT evict the cached PBS on this signal; doing so would tear
    down a healthy remote BrowserContext over a recoverable page-level state. Only the
    explicit driver-level ``Connection closed while reading from the driver`` should
    trigger the evict + reconnect path."""
    from skyvern.exceptions import FailedToNavigateToUrl

    manager = RealBrowserManager()
    stale = _stale_pbs_browser_state(
        navigate_exc=FailedToNavigateToUrl(
            url="https://example.com",
            error_message="Page.goto: Target page, context or browser has been closed",
        )
    )
    workflow_run = make_workflow_run("wfr_pbs")

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=stale)
        mock_app.PERSISTENT_SESSIONS_MANAGER.evict_cached_browser_state = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        with pytest.raises(FailedToNavigateToUrl, match="Target page, context or browser"):
            await manager.get_or_create_for_workflow_run(
                workflow_run=workflow_run,
                url="https://example.com",
                browser_session_id="pbs_abc",
            )

    mock_app.PERSISTENT_SESSIONS_MANAGER.evict_cached_browser_state.assert_not_awaited()
    # Single get_browser_state — no refetch, since the page-only close did not trigger
    # the evict + reconnect path.
    assert mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.await_count == 1


@pytest.mark.asyncio
async def test_pbs_recovery_path_passes_expected_state_to_public_evict() -> None:
    """The cached-CDP recovery path's evict must pass the stale ``BrowserState`` so the
    manager can skip closing a fresh wrapper that a parallel coroutine just stored.
    Without the ``expected`` argument, the public evict would unconditionally pop and
    close whatever sits in the cache."""
    from skyvern.exceptions import FailedToNavigateToUrl

    manager = RealBrowserManager()
    stale = _stale_pbs_browser_state(
        navigate_exc=FailedToNavigateToUrl(
            url="https://example.com",
            error_message="Page.goto: Connection closed while reading from the driver",
        )
    )
    fresh = _fresh_pbs_browser_state()
    workflow_run = make_workflow_run("wfr_pbs")

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch("skyvern.webeye.real_browser_manager.rebind_download_dir", new_callable=AsyncMock),
    ):
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(side_effect=[stale, fresh])
        mock_app.PERSISTENT_SESSIONS_MANAGER.evict_cached_browser_state = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id="pbs_abc",
        )

    mock_app.PERSISTENT_SESSIONS_MANAGER.evict_cached_browser_state.assert_awaited_once_with(
        "pbs_abc",
        organization_id=workflow_run.organization_id,
        expected=stale,
    )


@pytest.mark.asyncio
async def test_pbs_recovery_path_rebinds_download_dir_on_fresh_browser() -> None:
    """The cached-CDP recovery path replaces ``browser_state`` with a fresh CDP
    connection from ``PERSISTENT_SESSIONS_MANAGER``. The fresh state inherits the
    persistent-session download path, so artifacts would otherwise be saved under the
    session binding instead of the workflow-run directory. The manager must rerun
    ``rebind_download_dir`` on the fresh browser before retrying navigation."""
    from skyvern.exceptions import FailedToNavigateToUrl

    manager = RealBrowserManager()
    stale = _stale_pbs_browser_state(
        navigate_exc=FailedToNavigateToUrl(
            url="https://example.com",
            error_message="Page.goto: Connection closed while reading from the driver",
        )
    )
    fresh = _fresh_pbs_browser_state()
    workflow_run = make_workflow_run("wfr_pbs")

    with (
        patch("skyvern.webeye.real_browser_manager.app") as mock_app,
        patch("skyvern.webeye.real_browser_manager.rebind_download_dir", new_callable=AsyncMock) as mock_rebind,
    ):
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(side_effect=[stale, fresh])
        mock_app.PERSISTENT_SESSIONS_MANAGER.evict_cached_browser_state = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id="pbs_abc",
        )

    assert mock_rebind.await_count == 2
    rebind_browsers = [call.args[0] for call in mock_rebind.await_args_list]
    assert stale.browser_context.browser in rebind_browsers
    assert fresh.browser_context.browser in rebind_browsers


@pytest.mark.asyncio
async def test_pbs_navigate_skips_recovery_when_manager_cannot_reconnect() -> None:
    """The cached-CDP evict+reconnect path only works against managers whose
    ``get_browser_state`` reconnects after an evict. ``DefaultPersistentSessionsManager``'s
    ``get_browser_state`` is a pure in-memory dict lookup — evicting drops the only
    BrowserState, the refetch returns None, and the recovery path re-raises with the
    cache already torn down (so ``close_session`` profile/video cleanup later finds
    nothing). Skip the evict when the manager reports it cannot reconnect; the original
    ``FailedToNavigateToUrl`` bubbles up unchanged and the cache is preserved."""
    from skyvern.exceptions import FailedToNavigateToUrl

    manager = RealBrowserManager()
    stale = _stale_pbs_browser_state(
        navigate_exc=FailedToNavigateToUrl(
            url="https://example.com",
            error_message="Page.goto: Connection closed while reading from the driver",
        )
    )
    workflow_run = make_workflow_run("wfr_pbs")

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=stale)
        mock_app.PERSISTENT_SESSIONS_MANAGER.evict_cached_browser_state = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()
        # Manager reports it cannot reconnect after evict (OSS default impl shape).
        mock_app.PERSISTENT_SESSIONS_MANAGER.supports_evict_and_reconnect = MagicMock(return_value=False)

        with pytest.raises(FailedToNavigateToUrl, match="Connection closed"):
            await manager.get_or_create_for_workflow_run(
                workflow_run=workflow_run,
                url="https://example.com",
                browser_session_id="pbs_abc",
            )

    mock_app.PERSISTENT_SESSIONS_MANAGER.evict_cached_browser_state.assert_not_awaited()
    # Single get_browser_state call — no refetch, since recovery was skipped.
    assert mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.await_count == 1


@pytest.mark.asyncio
async def test_pbs_recovery_falls_through_to_get_or_create_page_when_fresh_state_has_no_page() -> None:
    """When evict+reconnect succeeds but the fresh ``BrowserState`` has no working
    page (e.g. the prior context closed its last tab during the dead-CDP window, or
    the new connection landed on an empty target), the recovery path must NOT re-raise
    the original navigation error. The normal-path ``get_or_create_page`` below the
    PBS branch can produce a page and navigate to the URL — mirror that so a
    recoverable session is not failed."""
    from skyvern.exceptions import FailedToNavigateToUrl

    manager = RealBrowserManager()
    stale = _stale_pbs_browser_state(
        navigate_exc=FailedToNavigateToUrl(
            url="https://example.com",
            error_message="Page.goto: Connection closed while reading from the driver",
        )
    )
    fresh = _fresh_pbs_browser_state()
    # Fresh CDP connection has no current page.
    fresh.get_working_page = AsyncMock(return_value=None)
    workflow_run = make_workflow_run("wfr_pbs")

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(side_effect=[stale, fresh])
        mock_app.PERSISTENT_SESSIONS_MANAGER.evict_cached_browser_state = AsyncMock()
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        result = await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url="https://example.com",
            browser_session_id="pbs_abc",
        )

    assert result is fresh
    # The outer normal-path get_or_create_page must run with the URL so the
    # fresh CDP connection acquires a page and lands on the target.
    fresh.get_or_create_page.assert_awaited_once()
    create_call = fresh.get_or_create_page.await_args
    assert create_call.kwargs.get("url") == "https://example.com"
    # We never re-attempted navigate_to_url on the fresh state (no page to use).
    fresh.navigate_to_url.assert_not_awaited()


class _EngineUnderTestError(Exception):
    pass


class _EngineUnderTestTimeout(_EngineUnderTestError):
    pass


@pytest.mark.asyncio
async def test_create_browser_state_stamps_resolved_engine_selection() -> None:
    """The exact BrowserEngineSelection resolved at the manager's ownership boundary
    (get_or_resolve_engine_selection) must be the identical object pinned on the constructed
    RealBrowserState, so a run's recovery/classification code binds to THIS run's engine identity
    rather than a rebuilt or dropped selection."""
    manager = RealBrowserManager()
    fake_pw = MagicMock()
    selection = BrowserEngineSelection(
        name="engine-under-test",
        start_driver=AsyncMock(return_value=fake_pw),
        error_type=_EngineUnderTestError,
        timeout_error_type=_EngineUnderTestTimeout,
        metadata=BrowserEngineMetadata(name="engine-under-test", version="0.0.0"),
        selection_reason="test",
    )

    with (
        patch.object(manager, "get_or_resolve_engine_selection", AsyncMock(return_value=selection)),
        patch.object(
            real_browser_manager.BrowserContextFactory,
            "create_browser_context",
            AsyncMock(return_value=(MagicMock(), BrowserArtifacts(), None)),
        ) as create_browser_context,
    ):
        state = await manager._create_browser_state(workflow_run_id="wr_engine_stamp")

    assert state.engine_selection is selection
    assert state.pw is fake_pw
    selection.start_driver.assert_awaited_once()
    assert create_browser_context.await_args.kwargs["engine_selection"] is selection


@pytest.mark.asyncio
async def test_repair_forwards_pinned_engine_selection() -> None:
    selection = BrowserEngineSelection(
        name="engine-under-test",
        start_driver=AsyncMock(),
        error_type=_EngineUnderTestError,
        timeout_error_type=_EngineUnderTestTimeout,
        metadata=BrowserEngineMetadata(name="engine-under-test", version="0.0.0"),
        selection_reason="test",
    )
    state = RealBrowserState(
        pw=MagicMock(),
        browser_context=None,
        engine_selection=selection,
    )
    context = MagicMock()
    context.pages = []

    with (
        patch(
            "skyvern.webeye.real_browser_state.BrowserContextFactory.create_browser_context",
            AsyncMock(return_value=(context, BrowserArtifacts(), None)),
        ) as create_browser_context,
        patch.object(state, "get_working_page", AsyncMock(return_value=MagicMock())),
    ):
        await state.check_and_fix_state()

    assert create_browser_context.await_args.kwargs["engine_selection"] is selection


class _EngE(Exception):
    pass


class _EngT(_EngE):
    pass


def _engine_sel(
    name: str,
    *,
    boot_fallback: BrowserEngineSelection | None = None,
    start=None,
) -> BrowserEngineSelection:
    async def _ok_start() -> MagicMock:
        driver = MagicMock()
        driver.stop = AsyncMock()
        return driver

    return BrowserEngineSelection(
        name=name,
        start_driver=start or _ok_start,
        error_type=_EngE,
        timeout_error_type=_EngT,
        metadata=BrowserEngineMetadata(name=name, version=None),
        selection_reason="test",
        boot_fallback_selection=boot_fallback,
    )


def _install_owner(manager: RealBrowserManager, run_key: str, selection: BrowserEngineSelection):
    resolved = asyncio.get_event_loop().create_future()
    resolved.set_result(selection)
    owner = real_browser_manager._EngineSelectionOwner(resolved)
    manager._engine_owners[run_key] = owner
    return owner


def _failing_start(message: str):
    async def _boom():
        raise RuntimeError(message)

    return _boom


@pytest.mark.asyncio
async def test_boot_fallback_driver_start_failure_falls_back_once_and_repins() -> None:
    manager = RealBrowserManager()
    classical = _engine_sel("playwright")
    rustwright = _engine_sel("rustwright", boot_fallback=classical, start=_failing_start("start boom"))
    _install_owner(manager, "wr_1", rustwright)
    with (
        patch.object(manager, "get_or_resolve_engine_selection", AsyncMock(return_value=rustwright)),
        patch.object(
            real_browser_manager.BrowserContextFactory,
            "create_browser_context",
            AsyncMock(return_value=(MagicMock(), BrowserArtifacts(), None)),
        ),
    ):
        state = await manager._create_browser_state(workflow_run_id="wr_1", engine_run_key="wr_1")
    assert state.engine_selection.name == "playwright"
    pinned = manager._engine_owners["wr_1"].task.result()
    assert pinned.name == "playwright"
    assert pinned.boot_fallback_selection is None  # classical fallback carries no further fallback


@pytest.mark.asyncio
async def test_boot_fallback_context_bootstrap_error_falls_back_once() -> None:
    manager = RealBrowserManager()
    classical = _engine_sel("playwright")
    rustwright = _engine_sel("rustwright", boot_fallback=classical)
    _install_owner(manager, "wr_1", rustwright)

    calls: list[str] = []

    async def _create(pw, **kwargs):
        calls.append(kwargs["engine_selection"].name)
        if kwargs["engine_selection"].name == "rustwright":
            raise BrowserEngineBootstrapError("rustwright launch failed")
        return (MagicMock(), BrowserArtifacts(), None)

    with (
        patch.object(manager, "get_or_resolve_engine_selection", AsyncMock(return_value=rustwright)),
        patch.object(real_browser_manager.BrowserContextFactory, "create_browser_context", _create),
    ):
        state = await manager._create_browser_state(workflow_run_id="wr_1", engine_run_key="wr_1")
    assert state.engine_selection.name == "playwright"
    assert calls == ["rustwright", "playwright"]  # one hop only


@pytest.mark.asyncio
async def test_boot_fallback_stripped_on_successful_rustwright_commit() -> None:
    manager = RealBrowserManager()
    classical = _engine_sel("playwright")
    rustwright = _engine_sel("rustwright", boot_fallback=classical)
    _install_owner(manager, "wr_1", rustwright)
    with (
        patch.object(manager, "get_or_resolve_engine_selection", AsyncMock(return_value=rustwright)),
        patch.object(
            real_browser_manager.BrowserContextFactory,
            "create_browser_context",
            AsyncMock(return_value=(MagicMock(), BrowserArtifacts(), None)),
        ),
    ):
        state = await manager._create_browser_state(workflow_run_id="wr_1", engine_run_key="wr_1")
    assert state.engine_selection.name == "rustwright"
    # Commit-strip: a later same-run recreation reuses rustwright but can no longer fall back.
    pinned = manager._engine_owners["wr_1"].task.result()
    assert pinned.name == "rustwright"
    assert pinned.boot_fallback_selection is None


@pytest.mark.asyncio
async def test_classical_fallback_failure_propagates_with_no_second_hop() -> None:
    manager = RealBrowserManager()
    classical = _engine_sel("playwright", start=_failing_start("classical boom"))
    rustwright = _engine_sel("rustwright", boot_fallback=classical, start=_failing_start("rustwright boom"))
    _install_owner(manager, "wr_1", rustwright)
    with patch.object(manager, "get_or_resolve_engine_selection", AsyncMock(return_value=rustwright)):
        with pytest.raises(RuntimeError, match="classical boom"):
            await manager._create_browser_state(workflow_run_id="wr_1", engine_run_key="wr_1")


@pytest.mark.asyncio
async def test_non_fallback_driver_start_failure_propagates_unchanged() -> None:
    # Default path preserved: a selection with no boot fallback that fails to start propagates as before.
    manager = RealBrowserManager()
    plain = _engine_sel("playwright", start=_failing_start("start boom"))
    _install_owner(manager, "wr_1", plain)
    with patch.object(manager, "get_or_resolve_engine_selection", AsyncMock(return_value=plain)):
        with pytest.raises(RuntimeError, match="start boom"):
            await manager._create_browser_state(workflow_run_id="wr_1", engine_run_key="wr_1")


@pytest.mark.asyncio
async def test_repin_engine_selection_is_guarded_against_resurrection() -> None:
    manager = RealBrowserManager()
    # Missing owner: repin must not create one.
    manager._repin_engine_selection("absent", _engine_sel("playwright"))
    assert "absent" not in manager._engine_owners
    # Terminal owner: repin must not replace its task (no resurrection of a torn-down run).
    owner = _install_owner(manager, "wr_t", _engine_sel("rustwright"))
    owner.terminal = True
    original_task = owner.task
    manager._repin_engine_selection("wr_t", _engine_sel("playwright"))
    assert manager._engine_owners["wr_t"].task is original_task
    # Ephemeral resource (run_key None): no-op, no error.
    manager._repin_engine_selection(None, _engine_sel("playwright"))


def _fake_cleanup_state() -> MagicMock:
    state = MagicMock()
    state.close = AsyncMock(return_value=True)
    state.browser_context = None
    state.browser_artifacts.traces_dir = None
    state.browser_cleanup = object()
    return state


def _close_true_count(state: MagicMock) -> int:
    return sum(1 for call in state.close.await_args_list if call.kwargs.get("close_browser_on_completion") is True)


@contextmanager
def _live_workflow_run_contexts(*run_ids: str) -> Iterator[None]:
    """Control the process-local run-liveness signal the non-PBS sharing predicate consults: only
    the given run ids read as live, every other wr_ alias is treated as a ghost. Backs the C1 fix —
    a forward-synced parent key whose run is not live here must not veto the terminal close."""
    wcm = forge_app.WORKFLOW_CONTEXT_MANAGER
    live = set(run_ids)
    original = wcm.has_workflow_run_context
    wcm.has_workflow_run_context = lambda workflow_run_id: workflow_run_id in live
    try:
        yield
    finally:
        wcm.has_workflow_run_context = original


@pytest.mark.asyncio
async def test_cleanup_same_run_alias_and_ghost_alias_closes_browser_once() -> None:
    manager = RealBrowserManager()
    state = _fake_cleanup_state()
    manager.pages["wr_1"] = state
    manager.pages["tsk_1"] = state
    manager.pages["tsk_ghost"] = state

    result = await manager.cleanup_for_workflow_run("wr_1", ["tsk_1"], close_browser_on_completion=True)

    assert _close_true_count(state) == 1
    assert result.recording_finalized is True


@pytest.mark.asyncio
async def test_cleanup_ghost_only_alias_absent_from_task_list_still_closes() -> None:
    manager = RealBrowserManager()
    state = _fake_cleanup_state()
    manager.pages["wr_1"] = state
    manager.pages["tsk_ghost"] = state

    result = await manager.cleanup_for_workflow_run("wr_1", [], close_browser_on_completion=True)

    assert _close_true_count(state) == 1
    assert result.recording_finalized is True


@pytest.mark.asyncio
async def test_cleanup_genuine_live_parent_sharing_does_not_close_early() -> None:
    # A use_parent_browser_session parent that is genuinely live in THIS process shares the exact
    # BrowserState; the child's terminal cleanup must not close it out from under the live parent.
    manager = RealBrowserManager()
    state = _fake_cleanup_state()
    manager.pages["wr_child"] = state
    manager.pages["tsk_c"] = state
    manager.pages["wr_parent"] = state

    with _live_workflow_run_contexts("wr_child", "wr_parent"):
        result = await manager.cleanup_for_workflow_run("wr_child", ["tsk_c"], close_browser_on_completion=True)

    assert _close_true_count(state) == 0
    assert "wr_parent" in manager.pages
    assert result.recording_finalized is False


@pytest.mark.asyncio
async def test_cleanup_ghost_parent_alias_without_live_context_closes_once() -> None:
    # A ghost parent: a child whose parent ran in another process creates a fresh
    # state and forward-syncs pages[wr_parent] = state. The parent is NOT live in this process, so
    # its ghost alias must not veto the child's terminal close — otherwise the browser leaks.
    manager = RealBrowserManager()
    state = _fake_cleanup_state()
    manager.pages["wr_child"] = state
    manager.pages["tsk_c"] = state
    manager.pages["wr_parent"] = state  # forward-synced ghost; parent lives in another process

    with _live_workflow_run_contexts("wr_child"):  # only the child is live here
        result = await manager.cleanup_for_workflow_run("wr_child", ["tsk_c"], close_browser_on_completion=True)
        # The ghost parent key is not a live run, so it does not veto the close.
        assert manager._shared_with_another_workflow_run("wr_child", state) is False

    # The browser is closed exactly once instead of leaking behind the ghost parent veto.
    assert _close_true_count(state) == 1
    assert result.recording_finalized is True


@pytest.mark.asyncio
async def test_cleanup_synthetic_ghost_wr_alias_does_not_suppress_close() -> None:
    # A never-cleaned synthetic-run wr_ key (SDK action ghost) aliasing this state has no live
    # context and must not suppress the terminal close.
    manager = RealBrowserManager()
    state = _fake_cleanup_state()
    manager.pages["wr_1"] = state
    manager.pages["wr_synthetic_ghost"] = state

    with _live_workflow_run_contexts("wr_1"):
        result = await manager.cleanup_for_workflow_run("wr_1", [], close_browser_on_completion=True)

    assert _close_true_count(state) == 1
    assert result.recording_finalized is True


@pytest.mark.asyncio
async def test_cleanup_task_state_ghost_alias_still_closes_uniformly() -> None:
    # Task-loop uniformity: a distinct task-level state aliased only by a ghost tsk_ key (not a live
    # wr_ run) must still close exactly once — the task-loop predicate is the same liveness-qualified
    # ownership check as the run-level close.
    manager = RealBrowserManager()
    run_state = _fake_cleanup_state()
    task_state = _fake_cleanup_state()
    manager.pages["wr_1"] = run_state
    manager.pages["tsk_a"] = task_state
    manager.pages["tsk_ghost"] = task_state  # ghost alias of the distinct task state

    with _live_workflow_run_contexts("wr_1"):
        await manager.cleanup_for_workflow_run("wr_1", ["tsk_a"], close_browser_on_completion=True)

    assert _close_true_count(run_state) == 1
    assert _close_true_count(task_state) == 1


@pytest.mark.asyncio
async def test_cleanup_separate_task_browser_states_each_close_once() -> None:
    manager = RealBrowserManager()
    run_state = _fake_cleanup_state()
    task_state = _fake_cleanup_state()
    manager.pages["wr_1"] = run_state
    manager.pages["tsk_other"] = task_state
    manager.pages["tsk_run"] = run_state

    await manager.cleanup_for_workflow_run("wr_1", ["tsk_other", "tsk_run"], close_browser_on_completion=True)

    assert _close_true_count(run_state) == 1
    assert _close_true_count(task_state) == 1


@pytest.mark.asyncio
async def test_cleanup_child_workflow_pre_pop_permits_terminal_close() -> None:
    manager = RealBrowserManager()
    state = _fake_cleanup_state()
    manager.pages["wr_1"] = state
    manager.pages["wr_child"] = state

    await manager.cleanup_for_workflow_run(
        "wr_1", [], close_browser_on_completion=True, child_workflow_run_ids=["wr_child"]
    )

    assert _close_true_count(state) == 1


@pytest.mark.asyncio
async def test_cleanup_deferred_streams_ghost_alias_defers_close(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RealBrowserManager()
    state = _fake_cleanup_state()
    state.browser_artifacts.browser_session_dir = "/tmp/fake_profile"
    manager.pages["wr_1"] = state
    manager.pages["tsk_ghost"] = state

    defer_mock = MagicMock(return_value=True)
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.persist_session_cookies", AsyncMock())
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.stream_ref_active", lambda wrid: True)
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.set_deferred_close_params", defer_mock)

    await manager.cleanup_for_workflow_run("wr_1", [], close_browser_on_completion=True)

    defer_mock.assert_called_once_with("wr_1", True, release_driver=None)
    state.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_task_close_error_is_contained() -> None:
    manager = RealBrowserManager()
    run_state = _fake_cleanup_state()
    bad_state = _fake_cleanup_state()
    bad_state.close = AsyncMock(side_effect=RuntimeError("already closed"))
    ok_state = _fake_cleanup_state()
    manager.pages["wr_1"] = run_state
    manager.pages["tsk_bad"] = bad_state
    manager.pages["tsk_ok"] = ok_state

    result = await manager.cleanup_for_workflow_run("wr_1", ["tsk_bad", "tsk_ok"], close_browser_on_completion=True)

    assert _close_true_count(run_state) == 1
    assert _close_true_count(ok_state) == 1
    assert result.browser_state is run_state


@pytest.mark.asyncio
async def test_pbs_cleanup_release_only_even_with_zero_local_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    # PBS/non-PBS boundary: with NO live workflow context anywhere and a ghost wr_ alias present, a
    # PBS cleanup must still be release-only. The local-liveness predicate can never drive a PBS
    # terminal close — PBS lifetime is distributed, not decided by this process.
    workflow_run_id = "wr_pbs_zero_ctx"
    manager = RealBrowserManager()
    state = MagicMock()
    state.browser_artifacts.traces_dir = None
    state.close = AsyncMock(return_value=False)
    manager.pages[workflow_run_id] = state
    manager.pages["wr_ghost_local"] = state  # a wr_ alias; irrelevant under the PBS regime
    manager._persistent_session_leases[workflow_run_id] = _PersistentSessionLease(
        session_id="pbs_zero", organization_id="org_test", runnable_id=workflow_run_id, browser_state=state
    )
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock(return_value=True)
    monkeypatch.setattr(
        real_browser_manager,
        "app",
        MagicMock(
            PERSISTENT_SESSIONS_MANAGER=sessions,
            WORKFLOW_CONTEXT_MANAGER=MagicMock(has_workflow_run_context=MagicMock(return_value=False)),
        ),
    )

    await manager.cleanup_for_workflow_run(
        workflow_run_id,
        task_ids=[],
        close_browser_on_completion=False,
        browser_session_id="pbs_zero",
        organization_id="org_test",
    )

    state.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=False)
    sessions.release_browser_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_pbs_cross_process_stale_cleanup_is_release_cas_noop_and_never_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two processes, one PBS. Ownership has moved to process B (generation gen2, wrapper_b); process A
    # is stale (gen1, wrapper_a). Process A's cleanup must (a) never terminal-close the remote browser
    # and (b) release only through the expected-owner CAS, which rejects its stale identity — so
    # process B's live ownership is untouched (PBS distributed ownership).
    session_id = "pbs_shared"
    wrapper_b = object()
    current_owner = {"runnable_id": "wr_podB", "generation": "gen2", "wrapper": wrapper_b}

    async def release_cas(
        *,
        session_id: str,
        organization_id: str,
        expected_runnable_id: str | None,
        expected_runnable_generation_id: str | None,
        expected_browser_state: object,
    ) -> bool:
        return (
            expected_runnable_id == current_owner["runnable_id"]
            and expected_runnable_generation_id == current_owner["generation"]
            and expected_browser_state is current_owner["wrapper"]
        )

    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock(side_effect=release_cas)
    monkeypatch.setattr(real_browser_manager, "app", MagicMock(PERSISTENT_SESSIONS_MANAGER=sessions))

    mgr_a = RealBrowserManager()
    wrapper_a = MagicMock()
    wrapper_a.browser_artifacts.traces_dir = None
    wrapper_a.close = AsyncMock(return_value=False)
    mgr_a.pages["wr_podA"] = wrapper_a
    mgr_a._persistent_session_leases["wr_podA"] = _PersistentSessionLease(
        session_id=session_id,
        organization_id="org_test",
        runnable_id="wr_podA",
        runnable_generation_id="gen1",
        browser_state=wrapper_a,
    )

    await mgr_a.cleanup_for_workflow_run(
        "wr_podA",
        task_ids=[],
        close_browser_on_completion=False,
        browser_session_id=session_id,
        organization_id="org_test",
    )

    # The stale Pod never terminal-closes the remote browser.
    wrapper_a.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=False)
    # It releases with its OWN stale expected-owner tuple, which the CAS rejects.
    release_call = sessions.release_browser_session.await_args
    assert release_call.kwargs["expected_runnable_id"] == "wr_podA"
    assert release_call.kwargs["expected_runnable_generation_id"] == "gen1"
    assert release_call.kwargs["expected_browser_state"] is wrapper_a
    assert (
        await release_cas(
            session_id=session_id,
            organization_id="org_test",
            expected_runnable_id="wr_podA",
            expected_runnable_generation_id="gen1",
            expected_browser_state=wrapper_a,
        )
        is False
    )
    # The live owner (Pod B, gen2, wrapper_b) is still accepted — ownership genuinely moved and holds.
    assert (
        await release_cas(
            session_id=session_id,
            organization_id="org_test",
            expected_runnable_id="wr_podB",
            expected_runnable_generation_id="gen2",
            expected_browser_state=wrapper_b,
        )
        is True
    )


@pytest.mark.asyncio
async def test_pbs_second_occupancy_rejected_leaves_no_local_lease() -> None:
    # A second concurrent runnable attempting to occupy a PBS already held by another runnable is
    # rejected by begin_session's occupancy CAS; the manager propagates and leaves no local lease.
    manager = RealBrowserManager()
    task = make_task("tsk_second", workflow_run_id=None)

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        configure_browser_context_acquired_hook(mock_app)
        mock_app.PERSISTENT_SESSIONS_MANAGER.begin_session = AsyncMock(
            side_effect=BrowserSessionAlreadyOccupiedError("pbs_busy", "wr_first_owner")
        )
        with pytest.raises(BrowserSessionAlreadyOccupiedError):
            await manager.get_or_create_for_task(task=task, browser_session_id="pbs_busy")

    assert manager.live_session_runnable_ids() == set()


@pytest.mark.asyncio
async def test_address_only_cleanup_preserves_remote_and_defers_local_driver_with_zero_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Broad PBS, remote-address form: a caller-provided remote browser has no DB session lease, so the
    # service gate hands cleanup close_browser_on_completion=False. Even with a ghost wr_ alias and NO
    # live workflow context anywhere, the manager must keep the remote browser alive — close(False) —
    # and pass release_driver=None so the state stops only its own per-run local driver via
    # release_driver_on_close. Local liveness must never terminal-close the durable remote browser.
    manager = RealBrowserManager()
    state = _fake_cleanup_state()
    manager.pages["wr_addr"] = state
    manager.pages["wr_ghost_local"] = state  # ghost wr_ alias; not a live run
    monkeypatch.setattr(
        real_browser_manager,
        "app",
        MagicMock(WORKFLOW_CONTEXT_MANAGER=MagicMock(has_workflow_run_context=MagicMock(return_value=False))),
    )

    await manager.cleanup_for_workflow_run("wr_addr", [], close_browser_on_completion=False)

    state.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=None)
    assert _close_true_count(state) == 0


@pytest.mark.asyncio
async def test_address_only_cross_pod_stale_cleanup_never_terminal_closes_durable_browser() -> None:
    # Two Pods hold different local wrappers for the same durable remote browser reached by
    # browser_address (no session id, no lease). Pod A's cleanup must never terminal-close the durable
    # browser and must not touch Pod B's wrapper — the remote browser is owned by neither process.
    mgr_a = RealBrowserManager()
    wrapper_a = _fake_cleanup_state()
    mgr_a.pages["wr_podA"] = wrapper_a

    wrapper_b = _fake_cleanup_state()  # Pod B's independent wrapper for the same remote browser

    with _live_workflow_run_contexts():  # nothing is live in Pod A's process
        await mgr_a.cleanup_for_workflow_run("wr_podA", [], close_browser_on_completion=False)

    # The durable remote browser is preserved: close(False), never a terminal close.
    wrapper_a.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=None)
    assert _close_true_count(wrapper_a) == 0
    # Pod B's wrapper is entirely untouched by Pod A's cleanup.
    wrapper_b.close.assert_not_awaited()


def test_the_browser_manager_never_drags_in_the_http_route_layer() -> None:
    """Importing this module once cost ~476MB at first measurement, because one import reached into
    the `routes` package for a streaming registry and `routes/__init__` eagerly builds the whole API
    surface (FastAPI, the copilot stack, the openai-agents SDK). Every worker pod paid that to run
    zero routes. The registry now lives in `skyvern.forge.sdk.streaming`; this pins the graph so the
    edge cannot quietly return. A subprocess because the property is about a FRESH interpreter --
    in-process, some other test may already have imported routes.
    """
    import subprocess
    import sys

    probe = (
        "import sys; import skyvern.webeye.real_browser_manager; "
        "leaked = [m for m in sys.modules if m == 'skyvern.forge.sdk.routes' or m == 'agents']; "
        "sys.exit(2 if leaked else 0)"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, f"route layer leaked into the browser manager import graph\n{result.stderr[-800:]}"


def _browser_state_for_snapshot(artifacts: list[VideoArtifact]) -> MagicMock:
    browser_state = MagicMock()
    browser_state.browser_artifacts.video_artifacts = artifacts
    return browser_state


def test_snapshot_recording_prefixes_empty_when_no_artifacts() -> None:
    manager = RealBrowserManager()
    browser_state = _browser_state_for_snapshot([])
    assert manager.snapshot_recording_prefixes(browser_state=browser_state, task_id="t") == []


def test_snapshot_recording_prefixes_plans_growing_webm(tmp_path) -> None:
    """The plan captures the current on-disk length as the snapshot bound, not the live EOF."""
    manager = RealBrowserManager()
    src = tmp_path / "rec.webm"
    src.write_bytes(b"x" * 321)
    artifact = VideoArtifact(video_path=str(src), video_artifact_id="vid-0")
    browser_state = _browser_state_for_snapshot([artifact])

    plan = manager.snapshot_recording_prefixes(browser_state=browser_state, task_id="t")

    src.write_bytes(b"x" * 10_000)  # a snapshot taken now must not be enlarged by later growth
    assert plan == [RecordingPrefixSnapshot(video_artifact_id="vid-0", path=str(src), prefix_len=321)]


def test_snapshot_recording_prefixes_none_when_non_webm(tmp_path) -> None:
    manager = RealBrowserManager()
    src = tmp_path / "rec.mp4"
    src.write_bytes(b"mp4")
    artifact = VideoArtifact(video_path=str(src), video_artifact_id="vid-0")
    browser_state = _browser_state_for_snapshot([artifact])

    assert manager.snapshot_recording_prefixes(browser_state=browser_state, task_id="t") is None


def test_snapshot_recording_prefixes_none_when_missing_artifact_id(tmp_path) -> None:
    manager = RealBrowserManager()
    src = tmp_path / "rec.webm"
    src.write_bytes(b"x")
    artifact = VideoArtifact(video_path=str(src), video_artifact_id=None)
    browser_state = _browser_state_for_snapshot([artifact])

    assert manager.snapshot_recording_prefixes(browser_state=browser_state, task_id="t") is None


def test_snapshot_recording_prefixes_none_when_path_absent(tmp_path) -> None:
    manager = RealBrowserManager()
    artifact = VideoArtifact(video_path=str(tmp_path / "missing.webm"), video_artifact_id="vid-0")
    browser_state = _browser_state_for_snapshot([artifact])

    assert manager.snapshot_recording_prefixes(browser_state=browser_state, task_id="t") is None

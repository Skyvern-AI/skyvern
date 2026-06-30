"""
Tests for RealBrowserManager cache behavior (regression coverage for PR #9020).

PR #9020 introduced a regression where the self.pages cache check was gated
behind `if not browser_session_id:`, causing PBS workflow runs to skip the cache
on every call and re-invoke navigate_to_url() on every step.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.browser_artifacts import BrowserArtifacts, VideoArtifact
from skyvern.webeye.browser_factory import set_popup_video_listener
from skyvern.webeye.real_browser_manager import RealBrowserManager
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
        mock_app.AGENT_FUNCTION.merge_proxy_session_extra_http_headers.side_effect = _merge_cloud_proxy_session_headers
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
async def test_task_browser_inherits_session_proxy_pin_when_no_browser_state() -> None:
    manager = RealBrowserManager()
    task = make_task("tsk_1", proxy_location="RESIDENTIAL")
    task.extra_http_headers = {"X-Test": "1"}
    new_browser_state = MagicMock()
    new_browser_state.get_or_create_page = AsyncMock()

    session = make_session(proxy_location="RESIDENTIAL_ISP", proxy_session_id="abc1234567")

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
        mock_app.AGENT_FUNCTION.merge_proxy_session_extra_http_headers.side_effect = _merge_cloud_proxy_session_headers
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
async def test_workflow_run_browser_inherits_session_proxy_pin_when_no_browser_state() -> None:
    manager = RealBrowserManager()
    workflow_run = make_workflow_run("wfr_1")
    workflow_run.extra_http_headers = {"X-Test": "1"}

    new_browser_state = MagicMock()
    new_browser_state.get_or_create_page = AsyncMock()

    session = make_session(proxy_location="RESIDENTIAL_ISP", proxy_session_id="abc1234567")

    with patch("skyvern.webeye.real_browser_manager.app") as mock_app:
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
async def test_get_video_artifacts_finalize_true_invokes_ffmpeg(tmp_path) -> None:
    """The default (finalize=True) path remuxes via ffmpeg so the final upload has Duration + Cues."""
    src = tmp_path / "recording.webm"
    src.write_bytes(b"raw-webm-bytes")
    browser_state = _make_browser_state_with_video(str(src))

    with patch("skyvern.webeye.real_browser_manager.finalize_webm", new=AsyncMock(return_value=b"remuxed")) as m:
        artifacts = await RealBrowserManager().get_video_artifacts(browser_state=browser_state)

    m.assert_awaited_once_with(str(src))
    assert artifacts[0].video_data == b"remuxed"


@pytest.mark.asyncio
async def test_get_video_artifacts_finalize_false_skips_ffmpeg(tmp_path) -> None:
    """finalize=False is the per-step-snapshot path: read raw bytes, never spawn ffmpeg.

    This is what prevents long browser tasks from firing one ffmpeg subprocess per step
    (the step-sync runs while the recording file is still open — remux is pointless there).
    """
    src = tmp_path / "recording.webm"
    src.write_bytes(b"partial-webm-bytes")
    browser_state = _make_browser_state_with_video(str(src))

    with patch("skyvern.webeye.real_browser_manager.finalize_webm", new=AsyncMock()) as m:
        artifacts = await RealBrowserManager().get_video_artifacts(browser_state=browser_state, finalize=False)

    m.assert_not_awaited()
    assert artifacts[0].video_data == b"partial-webm-bytes"


@pytest.mark.asyncio
async def test_get_video_artifacts_non_webm_skips_ffmpeg(tmp_path) -> None:
    """Non-WebM container files (e.g. fully-formed MP4 from a remote source)
    are container-valid already; remuxing them through ``finalize_webm`` would
    corrupt the file. The extension-based short-circuit reads them raw."""
    src = tmp_path / "recording.mp4"
    src.write_bytes(b"mp4-bytes")
    browser_state = _make_browser_state_with_video(str(src))

    with patch("skyvern.webeye.real_browser_manager.finalize_webm", new=AsyncMock()) as m:
        artifacts = await RealBrowserManager().get_video_artifacts(browser_state=browser_state)

    m.assert_not_awaited()
    assert artifacts[0].video_data == b"mp4-bytes"


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

    # Let the ensure_future tasks run
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


@pytest.mark.asyncio
async def test_cleanup_persists_session_cookies_when_close_deferred_for_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active CDP streams defer the browser close, so cleanup must snapshot session cookies before
    store_browser_session archives the dir — the deferred close runs too late."""
    manager = RealBrowserManager()
    browser_state = MagicMock()
    browser_state.browser_artifacts.traces_dir = None
    browser_state.browser_artifacts.browser_session_dir = "/tmp/fake_profile"
    browser_state.close = AsyncMock()
    manager.pages["wfr_streamed"] = browser_state

    persist_mock = AsyncMock()
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.persist_session_cookies", persist_mock)
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.stream_ref_active", lambda wrid: True)
    monkeypatch.setattr("skyvern.webeye.real_browser_manager.set_deferred_close_params", lambda *a, **k: None)

    await manager.cleanup_for_workflow_run("wfr_streamed", task_ids=[], close_browser_on_completion=True)

    persist_mock.assert_awaited_once_with(browser_state.browser_context, "/tmp/fake_profile")
    browser_state.close.assert_not_awaited()


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
        mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=pbs_state)
        mock_app.PERSISTENT_SESSIONS_MANAGER.set_browser_state = AsyncMock()

        await manager.get_or_create_for_workflow_run(
            workflow_run=workflow_run,
            url=None,
            browser_session_id="bs_adopt",
        )

    mock_rebind.assert_awaited_once_with(adopted_browser, run_id="wfr_adopt")


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

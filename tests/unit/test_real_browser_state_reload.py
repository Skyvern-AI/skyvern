"""Unit tests for RealBrowserState.reload_page degradation and scoped usage.

Covers SKY-10476: extraction scrape reload must degrade through
load → domcontentloaded → commit instead of hard-failing on SPA pages.
Degradation is scoped to extraction tasks only.
"""

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.constants import ScrapeType
from skyvern.exceptions import FailedToReloadPage
from skyvern.forge.agent import ForgeAgent
from skyvern.webeye.browser_artifacts import BrowserArtifacts, DownloadBinding
from skyvern.webeye.real_browser_state import RealBrowserState

_AGENT_MODULE = "skyvern.forge.agent"


@pytest.fixture
def browser_state() -> RealBrowserState:
    state = RealBrowserState.__new__(RealBrowserState)
    state.engine_selection = None
    return state


def _make_page(reload_side_effect=None) -> MagicMock:
    page = MagicMock()
    page.url = "https://example.test/spa"
    page.reload = AsyncMock(side_effect=reload_side_effect)
    return page


@pytest.mark.asyncio
async def test_reload_page_default_raises_on_timeout(browser_state: RealBrowserState) -> None:
    """Default reload_page (no degradation) raises FailedToReloadPage on timeout — existing behavior."""
    page = _make_page(PlaywrightTimeoutError("Page.reload: Timeout 60000ms exceeded"))

    with patch.object(browser_state, "_RealBrowserState__assert_page", return_value=page):
        with pytest.raises(FailedToReloadPage):
            await browser_state.reload_page()

    page.reload.assert_called_once()
    call_kwargs = page.reload.call_args
    assert "wait_until" not in (call_kwargs.kwargs or {})


@pytest.mark.asyncio
async def test_reload_page_default_succeeds_unchanged(browser_state: RealBrowserState) -> None:
    """Default reload_page succeeds without passing wait_until — no behavior change."""
    page = _make_page()
    browser_state._wait_for_settle = AsyncMock()
    browser_state._wait_for_challenge_solver = AsyncMock()

    with patch.object(browser_state, "_RealBrowserState__assert_page", return_value=page):
        await browser_state.reload_page()

    page.reload.assert_called_once()
    call_kwargs = page.reload.call_args
    assert "wait_until" not in (call_kwargs.kwargs or {})


@pytest.mark.asyncio
async def test_reload_page_degradation_succeeds_on_domcontentloaded(browser_state: RealBrowserState) -> None:
    """Degradation mode: load times out, domcontentloaded succeeds."""
    strategies_tried: list[str] = []

    async def fake_reload(timeout: int, wait_until: str = "load") -> None:
        strategies_tried.append(wait_until)
        if wait_until == "load":
            raise PlaywrightTimeoutError("Page.reload: Timeout 60000ms exceeded")

    page = _make_page(fake_reload)
    browser_state._wait_for_settle = AsyncMock()
    browser_state._wait_for_challenge_solver = AsyncMock()

    with patch.object(browser_state, "_RealBrowserState__assert_page", return_value=page):
        await browser_state.reload_page(degradation=True)

    assert strategies_tried == ["load", "domcontentloaded"]


@pytest.mark.asyncio
async def test_reload_page_degradation_succeeds_on_commit(browser_state: RealBrowserState) -> None:
    """Degradation mode: load and domcontentloaded time out, commit succeeds."""
    strategies_tried: list[str] = []

    async def fake_reload(timeout: int, wait_until: str = "load") -> None:
        strategies_tried.append(wait_until)
        if wait_until in ("load", "domcontentloaded"):
            raise PlaywrightTimeoutError(f"Page.reload: Timeout 60000ms exceeded ({wait_until})")

    page = _make_page(fake_reload)
    browser_state._wait_for_settle = AsyncMock()
    browser_state._wait_for_challenge_solver = AsyncMock()

    with patch.object(browser_state, "_RealBrowserState__assert_page", return_value=page):
        await browser_state.reload_page(degradation=True)

    assert strategies_tried == ["load", "domcontentloaded", "commit"]


@pytest.mark.asyncio
async def test_reload_page_degradation_raises_when_all_strategies_fail(browser_state: RealBrowserState) -> None:
    """Degradation mode: all strategies fail, raises FailedToReloadPage."""

    async def always_timeout(timeout: int, wait_until: str = "load") -> None:
        raise PlaywrightTimeoutError(f"Page.reload: Timeout 60000ms exceeded ({wait_until})")

    page = _make_page(always_timeout)

    with patch.object(browser_state, "_RealBrowserState__assert_page", return_value=page):
        with pytest.raises(FailedToReloadPage):
            await browser_state.reload_page(degradation=True)

    assert page.reload.call_count == 3


@pytest.mark.asyncio
async def test_reload_page_degradation_succeeds_on_first_try(browser_state: RealBrowserState) -> None:
    """Degradation mode: load succeeds immediately, no degradation needed."""
    strategies_tried: list[str] = []

    async def fake_reload(timeout: int, wait_until: str = "load") -> None:
        strategies_tried.append(wait_until)

    page = _make_page(fake_reload)
    browser_state._wait_for_settle = AsyncMock()
    browser_state._wait_for_challenge_solver = AsyncMock()

    with patch.object(browser_state, "_RealBrowserState__assert_page", return_value=page):
        await browser_state.reload_page(degradation=True)

    assert strategies_tried == ["load"]


# --- Scrape retry integration: RELOAD always uses degradation ---


def _make_agent() -> ForgeAgent:
    return ForgeAgent.__new__(ForgeAgent)


def _make_browser_state_mock() -> MagicMock:
    bs = MagicMock()
    bs.reload_page = AsyncMock()
    bs.scrape_website = AsyncMock(return_value=MagicMock())
    return bs


@pytest.mark.asyncio
async def test_scrape_with_type_reload_uses_degradation() -> None:
    """Scrape retry RELOAD always passes degradation=True."""
    agent = _make_agent()
    bs = _make_browser_state_mock()
    task = MagicMock()
    task.url = "https://example.test"
    step = MagicMock()
    mock_app = MagicMock()

    with patch(f"{_AGENT_MODULE}.app", mock_app):
        await agent._scrape_with_type(
            task=task,
            step=step,
            browser_state=bs,
            scrape_type=ScrapeType.RELOAD,
            engine=MagicMock(),
        )

    bs.reload_page.assert_called_once_with(degradation=True)


@pytest.mark.asyncio
async def test_scrape_with_type_normal_no_reload_call() -> None:
    """NORMAL scrape type does not call reload_page at all."""
    agent = _make_agent()
    bs = _make_browser_state_mock()
    task = MagicMock()
    task.url = "https://example.test"
    step = MagicMock()
    mock_app = MagicMock()

    with patch(f"{_AGENT_MODULE}.app", mock_app):
        await agent._scrape_with_type(
            task=task,
            step=step,
            browser_state=bs,
            scrape_type=ScrapeType.NORMAL,
            engine=MagicMock(),
        )

    bs.reload_page.assert_not_called()


def _reconnect_state(binding: DownloadBinding) -> RealBrowserState:
    state = RealBrowserState(pw=AsyncMock(), browser_context=MagicMock())
    state.engine_selection = MagicMock()
    state.engine_selection.start_driver = AsyncMock(return_value=AsyncMock())
    state.set_working_page = AsyncMock()
    state.browser_artifacts = BrowserArtifacts(download_binding=binding)

    async def _factory_rebuild(**kwargs: object) -> None:
        # Model the authoritative creator seam: check_and_fix_state forwards the binding and the factory
        # stamps it on the fresh artifacts (there is no post-hoc marker override on the state).
        forwarded = kwargs.get("download_binding")
        stamped = forwarded if isinstance(forwarded, DownloadBinding) else DownloadBinding.RUN_DIR
        state.browser_artifacts = BrowserArtifacts(download_binding=stamped)

    state.check_and_fix_state = _factory_rebuild  # type: ignore[method-assign]
    return state


@pytest.mark.asyncio
async def test_reconnect_preserves_session_dir_binding() -> None:
    state = _reconnect_state(DownloadBinding.SESSION_DIR)
    await state.reconnect(workflow_run_id="wr-1")
    assert state.browser_artifacts.download_binding == DownloadBinding.SESSION_DIR


@pytest.mark.asyncio
async def test_reconnect_keeps_run_dir_binding() -> None:
    state = _reconnect_state(DownloadBinding.RUN_DIR)
    await state.reconnect(workflow_run_id="wr-1")
    assert state.browser_artifacts.download_binding == DownloadBinding.RUN_DIR


def _reconnect_state_capturing_check(binding: DownloadBinding, captured: dict[str, object]) -> RealBrowserState:
    state = RealBrowserState(pw=AsyncMock(), browser_context=MagicMock())
    state.engine_selection = MagicMock()
    state.engine_selection.start_driver = AsyncMock(return_value=AsyncMock())
    state.set_working_page = AsyncMock()
    state.browser_artifacts = BrowserArtifacts(download_binding=binding)

    async def _capture(**kwargs: object) -> None:
        captured.update(kwargs)

    state.check_and_fix_state = _capture  # type: ignore[method-assign]
    return state


@pytest.mark.asyncio
async def test_reconnect_threads_session_dir_binding_into_check_and_fix_state() -> None:
    """Metadata restore alone is not enough — reconnect must hand the SESSION_DIR binding to
    check_and_fix_state so the factory skips the run-dir setDownloadBehavior rebind and preserves the
    provider-selected destination instead of re-pointing and relabeling afterward."""
    captured: dict[str, object] = {}
    state = _reconnect_state_capturing_check(DownloadBinding.SESSION_DIR, captured)
    await state.reconnect(workflow_run_id="wr-1")
    assert captured.get("download_binding") == DownloadBinding.SESSION_DIR


@pytest.mark.asyncio
async def test_reconnect_threads_run_dir_binding_into_check_and_fix_state() -> None:
    """A RUN_DIR session threads RUN_DIR so the ordinary run-scoped rebind still fires on reconnect."""
    captured: dict[str, object] = {}
    state = _reconnect_state_capturing_check(DownloadBinding.RUN_DIR, captured)
    await state.reconnect(workflow_run_id="wr-1")
    assert captured.get("download_binding") == DownloadBinding.RUN_DIR


def _recreation_state(
    binding: DownloadBinding, captured: dict[str, object]
) -> tuple[RealBrowserState, Callable[..., Awaitable[tuple[object, BrowserArtifacts, object]]]]:
    # No browser context forces the check_and_fix_state recreation branch.
    state = RealBrowserState(pw=AsyncMock(), browser_context=None)
    state.engine_selection = None
    state.browser_artifacts = BrowserArtifacts(download_binding=binding)
    state.set_working_page = AsyncMock()
    state.get_working_page = AsyncMock(return_value=MagicMock())

    async def _fake_create(_playwright: object, **kwargs: object) -> tuple[object, BrowserArtifacts, object]:
        captured.update(kwargs)
        return MagicMock(pages=[MagicMock()]), BrowserArtifacts(), None

    return state, _fake_create


@pytest.mark.asyncio
async def test_check_and_fix_state_recreation_preserves_session_dir_when_binding_omitted() -> None:
    """Non-reconnect entry: a recreation entry point (get_or_create_page, retry paths) that calls
    check_and_fix_state() without an explicit binding must not downgrade a live SESSION_DIR state to the
    RUN_DIR default and rebind it off the provider-selected destination. The omitted binding derives
    from the state's own prior artifacts."""
    captured: dict[str, object] = {}
    state, fake_create = _recreation_state(DownloadBinding.SESSION_DIR, captured)

    with (
        patch(
            "skyvern.webeye.real_browser_state.BrowserContextFactory.create_browser_context",
            new=fake_create,
        ),
        patch("skyvern.webeye.real_browser_state.skyvern_context.current", return_value=None),
    ):
        await state.check_and_fix_state(workflow_run_id="wr-1")

    assert captured.get("download_binding") == DownloadBinding.SESSION_DIR


@pytest.mark.asyncio
async def test_check_and_fix_state_recreation_defaults_run_dir_for_run_dir_state() -> None:
    """A RUN_DIR state omitting the binding still recreates as RUN_DIR — the sticky-derive must not
    accidentally promote ordinary local/OSS/vendor states to SESSION_DIR."""
    captured: dict[str, object] = {}
    state, fake_create = _recreation_state(DownloadBinding.RUN_DIR, captured)

    with (
        patch(
            "skyvern.webeye.real_browser_state.BrowserContextFactory.create_browser_context",
            new=fake_create,
        ),
        patch("skyvern.webeye.real_browser_state.skyvern_context.current", return_value=None),
    ):
        await state.check_and_fix_state(workflow_run_id="wr-1")

    assert captured.get("download_binding") == DownloadBinding.RUN_DIR


@pytest.mark.asyncio
async def test_check_and_fix_state_explicit_binding_overrides_prior_artifacts() -> None:
    """An explicit binding still wins over the derived one (reconnect passes prior_download_binding)."""
    captured: dict[str, object] = {}
    state, fake_create = _recreation_state(DownloadBinding.RUN_DIR, captured)

    with (
        patch(
            "skyvern.webeye.real_browser_state.BrowserContextFactory.create_browser_context",
            new=fake_create,
        ),
        patch("skyvern.webeye.real_browser_state.skyvern_context.current", return_value=None),
    ):
        await state.check_and_fix_state(workflow_run_id="wr-1", download_binding=DownloadBinding.SESSION_DIR)

    assert captured.get("download_binding") == DownloadBinding.SESSION_DIR


@pytest.mark.asyncio
async def test_check_and_fix_state_recreation_stamps_forwarded_binding_on_assigned_artifacts() -> None:
    """Outer contract: on recreation, check_and_fix_state forwards the derived binding and the factory
    stamps it on the fresh artifacts, so the state ends up SESSION_DIR — with no later override that
    could mislabel a genuine provider change."""
    state = RealBrowserState(pw=AsyncMock(), browser_context=None)
    state.engine_selection = None
    state.browser_artifacts = BrowserArtifacts(download_binding=DownloadBinding.SESSION_DIR)
    state.set_working_page = AsyncMock()
    state.get_working_page = AsyncMock(return_value=MagicMock())

    async def _factory_stamps(_playwright: object, **kwargs: object) -> tuple[object, BrowserArtifacts, object]:
        forwarded = kwargs.get("download_binding")
        stamped = forwarded if isinstance(forwarded, DownloadBinding) else DownloadBinding.RUN_DIR
        return MagicMock(pages=[MagicMock()]), BrowserArtifacts(download_binding=stamped), None

    with (
        patch(
            "skyvern.webeye.real_browser_state.BrowserContextFactory.create_browser_context",
            new=_factory_stamps,
        ),
        patch("skyvern.webeye.real_browser_state.skyvern_context.current", return_value=None),
    ):
        await state.check_and_fix_state(workflow_run_id="wr-1")

    assert state.browser_artifacts.download_binding == DownloadBinding.SESSION_DIR

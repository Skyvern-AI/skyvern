"""Unit tests for skyvern.webeye.real_browser_state.navigate_to_url.

Covers SKY-8818: pages whose subresources never finish loading must still
succeed if the DOM has parsed, via progressive wait_until degradation.
"""

import socket
import threading
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.config import settings
from skyvern.exceptions import BlockedNavigationDestination, FailedToNavigateToUrl
from skyvern.forge import agent as agent_module
from skyvern.forge.agent import ForgeAgent
from skyvern.webeye import real_browser_state
from skyvern.webeye.navigation import navigate_with_retry
from skyvern.webeye.real_browser_state import RealBrowserState, _same_page_ignoring_fragment


@pytest.fixture
def browser_state(monkeypatch: pytest.MonkeyPatch) -> RealBrowserState:
    retry_sleep = AsyncMock()
    monkeypatch.setattr(real_browser_state, "navigate_with_retry", partial(navigate_with_retry, sleep=retry_sleep))
    # Bypass __init__; navigate_to_url only uses `self` for LOG context and _wait_for_settle.
    state = RealBrowserState.__new__(RealBrowserState)
    monkeypatch.setattr(state, "_wait_for_settle", AsyncMock())
    return state


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests assert on strategy order and exceptions, not wall-clock timing.
    # Collapse the retry backoff and the post-navigation settle to run instantly.
    monkeypatch.setattr("skyvern.webeye.navigation.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("skyvern.webeye.real_browser_state.asyncio.sleep", AsyncMock())

    def resolves_public(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_public)


def test_same_page_ignoring_fragment_matches_fragment_only_differences() -> None:
    assert _same_page_ignoring_fragment("https://example.test/results#section", "https://example.test/results") is True
    assert _same_page_ignoring_fragment("https://example.test/results/", "https://example.test/results") is True
    assert _same_page_ignoring_fragment("https://example.test/results?page=2", "https://example.test/results") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/private",
        "http://169.254.1.1/link-local",
        "http://127.0.0.1/loopback",
        "file:///etc/passwd",
    ],
)
async def test_navigate_to_url_rejects_unsafe_caller_destination(
    browser_state: RealBrowserState,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    page = MagicMock()
    page.goto = AsyncMock()

    with pytest.raises(BlockedNavigationDestination):
        await browser_state.navigate_to_url(page=page, url=url)

    page.goto.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["", "   ", "about:blank"])
async def test_navigate_to_url_treats_blank_targets_as_non_navigational(
    browser_state: RealBrowserState,
    url: str,
) -> None:
    # navigation._NON_NAVIGATIONAL_TARGETS: a blank target names no network destination, so the
    # destination guard deliberately lets it through rather than reporting a blocked host.
    page = MagicMock()
    page.goto = AsyncMock()

    await browser_state.navigate_to_url(page=page, url=url)

    page.goto.assert_awaited_once()


@pytest.mark.asyncio
async def test_navigation_accepts_public_caller_destination(
    browser_state: RealBrowserState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_loop_thread = threading.get_ident()
    resolver_threads: list[int] = []

    def resolves_public(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        resolver_threads.append(threading.get_ident())
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))]

    page = MagicMock()
    page.goto = AsyncMock()
    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_public)

    await browser_state.navigate_to_url(page=page, url="https://public.example.test/path")

    page.goto.assert_awaited_once()
    assert resolver_threads
    assert all(thread_id != event_loop_thread for thread_id in resolver_threads)


@pytest.mark.parametrize("http_status", [200, 404, 410])
@pytest.mark.asyncio
async def test_navigate_to_url_records_last_navigation_status(
    browser_state: RealBrowserState,
    monkeypatch: pytest.MonkeyPatch,
    http_status: int,
) -> None:
    # The Task V3 loop reads last_navigation_status to classify a dead/removed starting URL, so a
    # navigation must record the final response's HTTP status on the state (not leave it unset).
    def resolves_public(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_public)
    url = "https://public.example.test/path"
    response = SimpleNamespace(status=http_status, request=SimpleNamespace(url=url, redirected_from=None))
    page = MagicMock()
    page.goto = AsyncMock(return_value=response)

    await browser_state.navigate_to_url(page=page, url=url)

    assert browser_state.last_navigation_status == http_status


@pytest.mark.asyncio
async def test_navigate_to_url_accepts_allowed_loopback_destination(
    browser_state: RealBrowserState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = MagicMock()
    page.goto = AsyncMock()
    monkeypatch.setattr(settings, "ALLOWED_HOSTS", ["127.0.0.1"])

    await browser_state.navigate_to_url(page=page, url="http://127.0.0.1/local")

    page.goto.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_navigation_rejects_hostname_resolving_to_internal_address(
    browser_state: RealBrowserState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_loop_thread = threading.get_ident()
    resolver_threads: list[int] = []

    def resolves_internal(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        resolver_threads.append(threading.get_ident())
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.42", port or 0))]

    page = MagicMock()
    page.url = "https://93.184.216.34/current"
    page.goto = AsyncMock()
    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_internal)
    monkeypatch.setattr(browser_state, "must_get_working_page", AsyncMock(return_value=page))
    guarded_navigate = AsyncMock(wraps=browser_state.navigate_to_url)
    monkeypatch.setattr(browser_state, "navigate_to_url", guarded_navigate)

    step = SimpleNamespace(step_id="step-id", retry_index=0)
    task = SimpleNamespace(
        task_id="task-id",
        workflow_run_id=None,
        browser_session_id=None,
        browser_address=None,
        navigation_goal=None,
        navigation_payload=None,
        data_extraction_goal=None,
        complete_criterion=None,
        terminate_criterion=None,
        max_steps_per_run=settings.MAX_STEPS_PER_RUN,
        status="running",
        url="https://public-name.example.test/internal",
    )
    organization = SimpleNamespace(organization_id="organization-id", max_steps_per_run=None)
    agent = ForgeAgent.__new__(ForgeAgent)
    monkeypatch.setattr(
        agent,
        "initialize_execution_state",
        AsyncMock(return_value=(step, browser_state, MagicMock())),
    )
    fail_task = AsyncMock(return_value=False)
    monkeypatch.setattr(agent, "fail_task", fail_task)
    monkeypatch.setattr(
        agent_module.app,
        "DATABASE",
        SimpleNamespace(tasks=SimpleNamespace(get_task=AsyncMock(return_value=None))),
    )
    monkeypatch.setattr(
        agent_module.app,
        "AGENT_FUNCTION",
        SimpleNamespace(validate_step_execution=AsyncMock()),
    )

    with agent_module.skyvern_context.scoped(agent_module.skyvern_context.SkyvernContext()):
        await agent.execute_step(
            organization=organization,
            task=task,
            step=step,
            download_baseline_files=[],
        )

    guarded_navigate.assert_awaited_once_with(page=page, url=task.url)
    page.goto.assert_not_awaited()
    assert isinstance(fail_task.await_args.kwargs["exception"], BlockedNavigationDestination)
    assert resolver_threads
    assert all(thread_id != event_loop_thread for thread_id in resolver_threads)


@pytest.mark.asyncio
async def test_navigation_redirect_hop_rejects_hostname_resolving_to_internal_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_loop_thread = threading.get_ident()
    resolver_threads: list[int] = []

    def resolves_by_host(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        resolver_threads.append(threading.get_ident())
        ip = "10.0.0.42" if host == "redirect.example.test" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port or 0))]

    initial_request = SimpleNamespace(url="https://entry.example.test/start", redirected_from=None)
    redirect_request = SimpleNamespace(url="https://redirect.example.test/final", redirected_from=initial_request)
    navigate = AsyncMock(return_value=SimpleNamespace(request=redirect_request))
    settle = AsyncMock()
    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_by_host)

    with pytest.raises(BlockedNavigationDestination):
        await navigate_with_retry(
            navigate=navigate,
            url="https://entry.example.test/start",
            retry_times=3,
            settle=settle,
            sleep=AsyncMock(),
        )

    navigate.assert_awaited_once()
    settle.assert_not_awaited()
    assert resolver_threads
    assert all(thread_id != event_loop_thread for thread_id in resolver_threads)


@pytest.mark.asyncio
async def test_navigation_redirect_hop_accepts_public_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_loop_thread = threading.get_ident()
    resolver_threads: list[int] = []

    def resolves_public(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        resolver_threads.append(threading.get_ident())
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))]

    initial_request = SimpleNamespace(url="https://entry.example.test/start", redirected_from=None)
    redirect_request = SimpleNamespace(url="https://redirect.example.test/final", redirected_from=initial_request)
    navigate = AsyncMock(return_value=SimpleNamespace(request=redirect_request))
    settle = AsyncMock()
    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_public)

    await navigate_with_retry(
        navigate=navigate,
        url="https://entry.example.test/start",
        retry_times=3,
        settle=settle,
        sleep=AsyncMock(),
    )

    navigate.assert_awaited_once()
    settle.assert_awaited_once()
    assert resolver_threads
    assert all(thread_id != event_loop_thread for thread_id in resolver_threads)


@pytest.mark.asyncio
async def test_navigate_to_url_progresses_from_load_to_domcontentloaded(
    browser_state: RealBrowserState,
) -> None:
    """If wait_until='load' times out but 'domcontentloaded' succeeds, we succeed."""
    page = MagicMock()
    calls: list[str] = []

    async def fake_goto(url: str, timeout: int, wait_until: str = "load") -> None:
        calls.append(wait_until)
        if wait_until == "load":
            raise PlaywrightTimeoutError("Page.goto: Timeout 60000ms exceeded (load)")
        return None

    page.goto = AsyncMock(side_effect=fake_goto)

    await browser_state.navigate_to_url(
        page=page,
        url="https://example.test/slow-subresources",
        wait_until="load",
    )

    assert "load" in calls
    assert "domcontentloaded" in calls
    assert calls.index("domcontentloaded") > calls.index("load")


@pytest.mark.asyncio
async def test_navigate_to_url_raises_when_all_strategies_fail(
    browser_state: RealBrowserState,
) -> None:
    """If every wait_until strategy times out, raise FailedToNavigateToUrl."""
    page = MagicMock()

    async def always_timeout(url: str, timeout: int, wait_until: str = "load") -> None:
        raise PlaywrightTimeoutError(f"Page.goto: Timeout 60000ms exceeded ({wait_until})")

    page.goto = AsyncMock(side_effect=always_timeout)

    with pytest.raises(FailedToNavigateToUrl):
        await browser_state.navigate_to_url(
            page=page,
            url="https://example.test/fully-dead",
            wait_until="load",
        )


@pytest.mark.asyncio
async def test_navigate_to_url_honors_caller_supplied_wait_until_on_first_try(
    browser_state: RealBrowserState,
) -> None:
    """FileDownloadBlock passes wait_until='domcontentloaded' — it must be honored on first try."""
    page = MagicMock()
    calls: list[str] = []

    async def fake_goto(url: str, timeout: int, wait_until: str = "load") -> None:
        calls.append(wait_until)
        return None

    page.goto = AsyncMock(side_effect=fake_goto)

    await browser_state.navigate_to_url(
        page=page,
        url="https://example.test/fast",
        wait_until="domcontentloaded",
    )

    assert calls == ["domcontentloaded"]


@pytest.mark.asyncio
async def test_navigate_to_url_succeeds_on_first_try_with_default_load(
    browser_state: RealBrowserState,
) -> None:
    """Existing callers that use default wait_until='load' must keep working untouched."""
    page = MagicMock()
    calls: list[str] = []

    async def fake_goto(url: str, timeout: int, wait_until: str = "load") -> None:
        calls.append(wait_until)
        return None

    page.goto = AsyncMock(side_effect=fake_goto)

    await browser_state.navigate_to_url(
        page=page,
        url="https://example.test/fast-load",
    )

    assert calls == ["load"]

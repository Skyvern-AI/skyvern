"""Unit tests for skyvern.webeye.real_browser_state.navigate_to_url.

Covers SKY-8818: pages whose subresources never finish loading must still
succeed if the DOM has parsed, via progressive wait_until degradation.
"""

import asyncio
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
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye import real_browser_state
from skyvern.webeye.cdp_download_interceptor import CDPDownloadInterceptor
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


def _navigation_interceptor(proxy_auth_configured: bool) -> CDPDownloadInterceptor:
    return CDPDownloadInterceptor(
        proxy_username="user" if proxy_auth_configured else None,
        proxy_password="password" if proxy_auth_configured else None,
        network_egress_monitor=MagicMock(),
        redirect_hop_authorizer=MagicMock(),
    )


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
async def test_navigate_to_url_records_last_navigation_status_with_the_url_it_came_back_on(
    browser_state: RealBrowserState,
    monkeypatch: pytest.MonkeyPatch,
    http_status: int,
) -> None:
    # The Task V3 loop reads last_navigation_status to classify a dead/removed starting URL and names
    # the page in the customer's failure_reason, so the two must be recorded from the SAME response.
    # A page that moves during the settle or challenge-solver wait (a client-side redirect off a 404)
    # would otherwise leave a later read of page.url standing in for the URL the status belongs to --
    # telling the customer the redirect destination returned 404.
    def resolves_public(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_public)
    url = "https://public.example.test/path"
    redirected_to = "https://public.example.test/somewhere-else"
    response = SimpleNamespace(status=http_status, url=url, request=SimpleNamespace(url=url, redirected_from=None))
    page = MagicMock()
    page.url = url

    async def goto(*_args: object, **_kwargs: object) -> object:
        # The client-side redirect lands while the navigation is still settling.
        page.url = redirected_to
        return response

    page.goto = AsyncMock(side_effect=goto)

    await browser_state.navigate_to_url(page=page, url=url)

    assert browser_state.last_navigation_status == http_status
    assert browser_state.last_navigation_url == url


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_assigned", "proxy_auth_configured", "challenge_sources", "expected_reports"),
    [
        (True, True, None, 1),
        (True, True, ("Proxy",), 0),
        (True, True, ("Proxy", "Proxy"), 1),
        (True, True, ("Proxy", "Server"), 0),
        (True, True, ("Server",), 0),
        (True, False, None, 0),
        (False, True, ("Proxy", "Proxy"), 0),
    ],
)
async def test_navigation_failure_is_reported_only_for_authenticated_server_assigned_address(
    browser_state: RealBrowserState,
    monkeypatch: pytest.MonkeyPatch,
    server_assigned: bool,
    proxy_auth_configured: bool,
    challenge_sources: tuple[str, ...] | None,
    expected_reports: int,
) -> None:
    """Count proxy rejections or missing challenges, not successful proxy or target-site auth."""
    report = AsyncMock()
    monkeypatch.setattr(agent_module.app.AGENT_FUNCTION, "record_server_assigned_address_navigation_failure", report)
    browser_state._browser_address = "http://10.0.36.234:9225"
    interceptor = _navigation_interceptor(proxy_auth_configured)
    browser_state._browser_context = SimpleNamespace(
        _skyvern_cdp_download_interceptor=interceptor,
    )
    browser_state.begin_navigation()
    if challenge_sources is not None:
        cdp_session = MagicMock()
        cdp_session.send = AsyncMock()
        for source in challenge_sources:
            await interceptor._handle_auth_required(
                {
                    "requestId": f"request-{source}",
                    "authChallenge": {"source": source},
                    "request": {"url": "https://example.test/"},
                },
                cdp_session,
            )
    skyvern_context.set(
        SkyvernContext(
            workflow_run_id="wr_1",
            task_id="task_1",
            workflow_permanent_id="workflow_1",
            browser_address_is_server_assigned=server_assigned,
        ),
    )
    try:
        await browser_state.record_navigation_result(
            FailedToNavigateToUrl(
                url="https://example.test/",
                error_message="Page.goto: net::ERR_INVALID_AUTH_CREDENTIALS at https://example.test/",
                nav_error_code="net::ERR_INVALID_AUTH_CREDENTIALS",
            )
        )
    finally:
        skyvern_context.reset()

    assert report.await_count == expected_reports
    if expected_reports:
        assert report.await_args.kwargs == {
            "browser_address": "http://10.0.36.234:9225",
            "nav_error_code": "net::ERR_INVALID_AUTH_CREDENTIALS",
            "run_id": "wr_1",
            "workflow_permanent_id": "workflow_1",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy_auth_configured", [True, False])
async def test_success_clears_strikes_only_for_authenticated_server_assigned_address(
    browser_state: RealBrowserState,
    monkeypatch: pytest.MonkeyPatch,
    proxy_auth_configured: bool,
) -> None:
    report = AsyncMock()
    monkeypatch.setattr(agent_module.app.AGENT_FUNCTION, "record_server_assigned_address_navigation_success", report)
    browser_state._browser_address = "http://10.0.36.234:9225"
    interceptor = _navigation_interceptor(proxy_auth_configured)
    browser_state._browser_context = SimpleNamespace(
        _skyvern_cdp_download_interceptor=interceptor,
    )
    skyvern_context.set(SkyvernContext(workflow_run_id="wr_1", browser_address_is_server_assigned=True))
    try:
        await browser_state.record_navigation_result(None, url="https://example.test/")
    finally:
        skyvern_context.reset()

    if proxy_auth_configured:
        report.assert_awaited_once_with(browser_address="http://10.0.36.234:9225", run_id="wr_1")
    else:
        report.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["about:blank", "", "  "])
async def test_non_network_success_does_not_clear_strikes(
    browser_state: RealBrowserState,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    report = AsyncMock()
    monkeypatch.setattr(agent_module.app.AGENT_FUNCTION, "record_server_assigned_address_navigation_success", report)
    browser_state._browser_address = "http://10.0.36.234:9225"
    browser_state._browser_context = SimpleNamespace(
        _skyvern_cdp_download_interceptor=_navigation_interceptor(proxy_auth_configured=True),
    )
    skyvern_context.set(SkyvernContext(workflow_run_id="wr_1", browser_address_is_server_assigned=True))
    try:
        await browser_state.record_navigation_result(None, url=url)
    finally:
        skyvern_context.reset()

    report.assert_not_awaited()


@pytest.mark.asyncio
async def test_navigation_result_report_is_bounded(
    browser_state: RealBrowserState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_report(**_: object) -> None:
        await asyncio.sleep(10)

    report = AsyncMock(side_effect=slow_report)
    monkeypatch.setattr(agent_module.app.AGENT_FUNCTION, "record_server_assigned_address_navigation_success", report)
    monkeypatch.setattr(real_browser_state, "BROWSER_ADDRESS_RESULT_TIMEOUT_SECONDS", 0.01)
    browser_state._browser_address = "http://10.0.36.234:9225"
    browser_state._browser_context = SimpleNamespace(
        _skyvern_cdp_download_interceptor=_navigation_interceptor(proxy_auth_configured=True),
    )
    skyvern_context.set(SkyvernContext(workflow_run_id="wr_1", browser_address_is_server_assigned=True))
    try:
        await browser_state.record_navigation_result(None, url="https://example.test/")
    finally:
        skyvern_context.reset()

    report.assert_awaited_once()


@pytest.mark.asyncio
async def test_deferred_navigation_keeps_server_address_and_workflow_identity(
    browser_state: RealBrowserState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = MagicMock()
    monkeypatch.setattr(browser_state, "get_working_page", AsyncMock(return_value=page))

    result = await browser_state.get_or_create_page(
        browser_address="http://10.0.36.234:9225",
        task_id="task_1",
        navigation_run_id="wr_1",
    )

    assert result is page
    assert browser_state._browser_address == "http://10.0.36.234:9225"
    assert browser_state._navigation_run_id == "wr_1"


@pytest.mark.asyncio
async def test_a_failed_report_does_not_change_how_the_run_fails(
    browser_state: RealBrowserState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_module.app.AGENT_FUNCTION,
        "record_server_assigned_address_navigation_failure",
        AsyncMock(side_effect=RuntimeError("redis is down")),
    )
    browser_state._browser_address = "http://10.0.36.234:9225"
    browser_state._browser_context = SimpleNamespace(
        _skyvern_cdp_download_interceptor=_navigation_interceptor(proxy_auth_configured=True),
    )
    skyvern_context.set(SkyvernContext(workflow_run_id="wr_1", browser_address_is_server_assigned=True))
    try:
        await browser_state.record_navigation_result(
            FailedToNavigateToUrl(
                url="https://example.test/",
                error_message="Page.goto: net::ERR_INVALID_AUTH_CREDENTIALS",
                nav_error_code="net::ERR_INVALID_AUTH_CREDENTIALS",
            )
        )
    finally:
        skyvern_context.reset()

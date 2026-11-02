"""The scrape retry ladder must not reload over a main-frame navigation that is still in flight.

Reloading the committed page cancels the pending navigation; when the committed page is an
auto-submitting form, the reload re-submits that form.
"""

from __future__ import annotations

import asyncio
import gc
import threading
import time
import weakref
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from pyee import EventEmitter

from skyvern.config import settings
from skyvern.constants import ScrapeType
from skyvern.forge import agent as agent_mod
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.schemas.runs import RunEngine
from skyvern.webeye import real_browser_state as real_browser_state_mod
from skyvern.webeye.real_browser_state import RealBrowserState
from skyvern.webeye.scraper import scraper as scraper_mod
from skyvern.webeye.utils import document as document_mod
from skyvern.webeye.utils.page import SkyvernFrame
from tests.unit.helpers import make_organization, make_step, make_task


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


requires_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="Requires Playwright chromium (playwright install chromium)"
)

LANDING_MARKER = "Bravo dashboard ready"
SPINNER_CSS = (
    "<style>@keyframes r{to{transform:rotate(360deg)}}"
    ".spinner{width:40px;height:40px;border:4px solid #888;animation:r 1s linear infinite}</style>"
)


@dataclass
class _Site:
    signin_unload_delay_ms: int = 0
    landing_header_delay: float = 0.0
    landing_body_delay: float = 0.0
    landing_never_answers: bool = False
    hits: list[tuple[str, str]] = field(default_factory=list)

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p in self.hits if m == method and p.startswith(path))


def _html(body: str) -> bytes:
    return f"<html><head>{SPINNER_CSS}</head><body>{body}</body></html>".encode()


def _handler(site: _Site) -> type[BaseHTTPRequestHandler]:
    pages = {
        "/start": _html("<p>start</p>"),
        "/spinner-gone": _html(
            "<div id='s' class='spinner'></div><p>start</p>"
            "<script>setTimeout(() => document.getElementById('s').remove(), 1000)</script>"
        ),
        "/spinner-stuck": _html("<div class='spinner'></div><p>start</p>"),
        "/iframe-loading": _html("<p>start</p><iframe src='/hang'></iframe>"),
        "/wedge": _html("<p>start</p><script>setTimeout(() => { while (true) {} }, 300)</script>"),
    }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:
            site.hits.append(("GET", self.path))
            try:
                if self.path.startswith("/signin"):
                    # Hold the renderer in the old document after response headers arrive, so the
                    # response-to-commit race is exercised through Chromium's real navigation path.
                    unload = (
                        "addEventListener('unload', () => {"
                        f"const until = Date.now() + {site.signin_unload_delay_ms};"
                        "while (Date.now() < until) {} });"
                    )
                    self._send(
                        200,
                        (
                            "<html><body>Signing in<form id='f' method='POST' action='/assert'>"
                            "<input name='token' value='t'></form>"
                            f"<script>{unload if site.signin_unload_delay_ms else ''}"
                            "document.getElementById('f').submit()</script></body></html>"
                        ).encode(),
                        {"Content-Type": "text/html", "Permissions-Policy": "unload=(self)"},
                    )
                elif self.path.startswith("/hop"):
                    self._send(302, headers={"Location": "/landing"})
                elif self.path.startswith("/landing"):
                    if site.landing_never_answers:
                        time.sleep(120)
                        return
                    # Only the hand-off's landing is slow; a later reload of the landing answers at once.
                    if site.count("GET", "/landing") == 1:
                        time.sleep(site.landing_header_delay)
                    head = b"<html><head><title>Bravo</title></head><body><h1>Bravo</h1>"
                    tail = f"<p id='done'>{LANDING_MARKER}</p></body></html>".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(head) + len(tail)))
                    self.end_headers()
                    self.wfile.write(head)
                    self.wfile.flush()
                    site.hits.append(("SENT", "/landing"))
                    time.sleep(site.landing_body_delay)
                    self.wfile.write(tail)
                elif self.path.startswith("/moved"):
                    self._send(307, headers={"Location": "/slow"})
                elif self.path.startswith("/slow"):
                    time.sleep(2)
                    self._send(200, b"<html><body><p>slow</p></body></html>", {"Content-Type": "text/html"})
                elif self.path.startswith("/hang"):
                    time.sleep(60)
                elif self.path.startswith("/file"):
                    self._send(
                        200,
                        b"abc",
                        {
                            "Content-Type": "application/octet-stream",
                            "Content-Disposition": "attachment; filename=a.bin",
                        },
                    )
                elif self.path.startswith("/empty"):
                    self._send(204)
                elif self.path.startswith("/pushstate"):
                    self._send(
                        200,
                        b"<html><body><script>location.href = '/hang';"
                        b"setTimeout(() => history.pushState({}, '', '/pushstate#moved'), 300)</script></body></html>",
                        {"Content-Type": "text/html"},
                    )
                else:
                    self._send(200, pages.get(self.path, pages["/start"]), {"Content-Type": "text/html"})
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            site.hits.append(("POST", self.path))
            self._send(302, headers={"Location": "/hop"})

    return Handler


@pytest_asyncio.fixture
async def site_browser() -> AsyncIterator[tuple[_Site, str, Any]]:
    from playwright.async_api import async_playwright

    site = _Site()

    class _QuietServer(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request: Any, client_address: Any) -> None:
            pass

    server = _QuietServer(("127.0.0.1", 0), _handler(site))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-proxy-server", "--proxy-bypass-list=*", "--use-mock-keychain", "--password-store=basic"],
            proxy={"server": "direct://"},
        )
        context = await browser.new_context()
        try:
            yield site, base, context
        finally:
            if browser.is_connected():
                await browser.close()
            server.shutdown()


def _fast_scrape_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    # Production's scrape timeouts are tens of seconds; shrink them so the NORMAL rungs time out
    # against the pending navigation within the test's budget. The timeouts are the only change.
    monkeypatch.setattr(settings, "BROWSER_SCRAPING_BUILDING_ELEMENT_TREE_TIMEOUT_MS", 5000)
    monkeypatch.setattr(settings, "PAGE_READY_NETWORK_IDLE_TIMEOUT_MS", 300)
    monkeypatch.setattr(settings, "PAGE_READY_LOADING_INDICATOR_TIMEOUT_MS", 300)
    monkeypatch.setattr(settings, "PAGE_READY_DOM_STABILITY_TIMEOUT_MS", 300)
    monkeypatch.setattr(document_mod, "LOADER_ID_ATTACH_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(document_mod, "LOADER_ID_FRAME_TREE_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(document_mod, "LOADER_ID_DETACH_TIMEOUT_SECONDS", 0.5)


def _agent_and_kwargs(
    monkeypatch: pytest.MonkeyPatch, browser_state: RealBrowserState, page_ready_wait: bool = False
) -> tuple[ForgeAgent, dict[str, Any]]:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), navigation_goal="Open the dashboard", workflow_run_id="wr_nav")
    step = make_step(now, task, step_id="step-nav", status=StepStatus.running, order=1, output=None)
    agent = ForgeAgent()
    agent.async_operation_pool = MagicMock()
    monkeypatch.setattr(agent_mod, "resolve_transient_ui_capture_arm", AsyncMock())
    monkeypatch.setattr(
        agent_mod.app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", AsyncMock(return_value=False)
    )

    async def keep_elements(_frame: Any, _url: str, element_tree: list[dict]) -> list[dict]:
        return element_tree

    monkeypatch.setattr(object.__getattribute__(agent_mod.app, "_inst"), "scrape_exclude", None, raising=False)
    monkeypatch.setattr(
        agent_mod.app.AGENT_FUNCTION, "cleanup_element_tree_factory", MagicMock(return_value=keep_elements)
    )
    skyvern_context.set(
        SkyvernContext(
            task_id=task.task_id, workflow_run_id=task.workflow_run_id, enable_page_ready_wait=page_ready_wait
        )
    )
    return agent, {"task": task, "step": step, "browser_state": browser_state, "engine": RunEngine.skyvern_v1}


async def _browser_state(context: Any, base: str, path: str = "/start") -> tuple[Any, RealBrowserState]:
    page = await context.new_page()
    browser_state = RealBrowserState(pw=MagicMock(), browser_context=context, page=page)
    # A still-loading iframe holds back the load event, so do not wait for it.
    await page.goto(f"{base}{path}", wait_until="domcontentloaded")
    return page, browser_state


async def _start_sign_in(page: Any, base: str, site: _Site, browser_state: RealBrowserState | None = None) -> None:
    """Start the auto-POST hand-off and return once the server has seen the landing request.

    With ``browser_state``, also wait until its passive observer has been handed that navigation. The server can
    see the request before the browser delivers the page's request event, and only the event is product input.
    """
    try:
        await asyncio.wait_for(page.evaluate(f"location.href = '{base}/signin'"), 3)
    except Exception:
        pass
    deadline = time.monotonic() + 5
    while site.count("GET", "/landing") == 0:
        assert time.monotonic() < deadline, "the landing request never started"
        await asyncio.sleep(0.05)
    if browser_state is not None:
        navigation = browser_state._main_frame_navigations[page]
        while not navigation.is_pending:
            assert time.monotonic() < deadline + 10, "the observer never received the pending navigation"
            await asyncio.sleep(0.02)


def _spy_reload(monkeypatch: pytest.MonkeyPatch, browser_state: RealBrowserState) -> AsyncMock:
    reload_spy = AsyncMock(wraps=browser_state.reload_page)
    monkeypatch.setattr(browser_state, "reload_page", reload_spy)
    return reload_spy


@requires_chromium
@pytest.mark.asyncio
@pytest.mark.parametrize("page_ready_wait", [False, True], ids=["animation_wait", "page_ready_wait"])
async def test_reload_waits_for_document_commit_after_response_headers(
    site_browser: tuple[_Site, str, Any], monkeypatch: pytest.MonkeyPatch, page_ready_wait: bool
) -> None:
    site, base, context = site_browser
    site.signin_unload_delay_ms = 2000
    site.landing_header_delay = 0.5
    page, browser_state = await _browser_state(context, base)
    agent, kwargs = _agent_and_kwargs(monkeypatch, browser_state, page_ready_wait)
    headers = asyncio.Event()

    def on_response(response: Any) -> None:
        if response.url == f"{base}/landing":
            headers.set()

    page.on("response", on_response)
    await _start_sign_in(page, base, site)
    await asyncio.wait_for(headers.wait(), 5)
    assert page.url == f"{base}/signin", "the test must reach headers before the new document commits"
    scraped = await asyncio.wait_for(
        agent._scrape_with_type(**kwargs, scrape_type=ScrapeType.RELOAD, navigation_deadline=time.monotonic() + 15),
        25,
    )
    assert LANDING_MARKER in scraped.html
    assert site.count("POST", "/assert") == 1, site.hits
    assert site.count("GET", "/signin") == 1, site.hits
    assert page.url == f"{base}/landing"


def _record_scrape_order(monkeypatch: pytest.MonkeyPatch, browser_state: RealBrowserState) -> list[str]:
    """Record, in order, the navigation wait, frame injection, readiness checks and challenge-solver wait."""
    events: list[str] = []
    real_create_instance = SkyvernFrame.create_instance.__func__  # type: ignore[attr-defined]

    async def create_instance(cls: Any, frame: Any, *args: Any, **kwargs: Any) -> Any:
        # A Page has a main_frame; an iframe's Frame does not.
        events.append("inject_main" if hasattr(frame, "main_frame") else "inject_frame")
        return await real_create_instance(cls, frame, *args, **kwargs)

    monkeypatch.setattr(SkyvernFrame, "create_instance", classmethod(create_instance))
    for name in ("wait_for_page_ready", "safe_wait_for_animation_end"):
        original = getattr(SkyvernFrame, name)

        def readiness(self: Any, *args: Any, _name: str = name, _original: Any = original, **kwargs: Any) -> Any:
            events.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(SkyvernFrame, name, readiness)
    for name, label in (
        ("wait_for_main_frame_navigation", "navigation_wait"),
        ("_wait_for_challenge_solver", "solver"),
    ):
        original_method = getattr(browser_state, name, None)
        if original_method is None:
            continue

        async def recorded(*args: Any, _label: str = label, _original: Any = original_method, **kwargs: Any) -> Any:
            events.append(_label)
            return await _original(*args, **kwargs)

        monkeypatch.setattr(browser_state, name, recorded)
    return events


def _first_scrape(order: list[str]) -> list[str]:
    """The events of the first scrape: from its challenge-solver wait up to the next scrape's."""
    start = order.index("solver")
    rest = order[start + 1 :]
    return order[start : start + 1 + (rest.index("solver") if "solver" in rest else len(rest))]


def _record_rungs(monkeypatch: pytest.MonkeyPatch, agent: ForgeAgent) -> list[tuple[str, float]]:
    rungs: list[tuple[str, float]] = []
    real_scrape_with_type = agent._scrape_with_type

    async def recorded(*args: Any, **kwargs: Any) -> Any:
        rungs.append((str(kwargs["scrape_type"].value), time.monotonic()))
        return await real_scrape_with_type(*args, **kwargs)

    monkeypatch.setattr(agent, "_scrape_with_type", recorded)
    return rungs


@requires_chromium
@pytest.mark.asyncio
@pytest.mark.parametrize("page_ready_wait", [False, True], ids=["animation_wait", "page_ready_wait"])
async def test_the_first_scrape_waits_for_a_slow_landing_instead_of_resubmitting_the_sign_in_form(
    site_browser: tuple[_Site, str, Any], monkeypatch: pytest.MonkeyPatch, page_ready_wait: bool
) -> None:
    site, base, context = site_browser
    # Long enough that a scrape that does not wait times out while the landing is still pending.
    site.landing_header_delay = 20.0
    site.landing_body_delay = 2.0
    page, browser_state = await _browser_state(context, base)
    _fast_scrape_failures(monkeypatch)
    agent, kwargs = _agent_and_kwargs(monkeypatch, browser_state, page_ready_wait)
    reload_spy = _spy_reload(monkeypatch, browser_state)
    order = _record_scrape_order(monkeypatch, browser_state)
    rungs = _record_rungs(monkeypatch, agent)

    await _start_sign_in(page, base, site, browser_state)
    try:
        result = await agent.build_and_record_step_prompt(**kwargs, persist_artifacts=False)
    except Exception as e:
        result, ladder_error = None, e
    else:
        ladder_error = None

    assert site.count("POST", "/assert") == 1, f"sign-in form re-submitted: {site.hits}"
    assert site.count("GET", "/signin") == 1, f"sign-in page reloaded: {site.hits}"
    assert ladder_error is None, f"retry ladder failed: {ladder_error!r}"
    assert result is not None and page.url.endswith("/landing")
    # The landing body arrives after its headers; the scrape must see the late content, not just the commit.
    assert LANDING_MARKER in result.scraped_page.html
    # The scrape must report the document it read, not the hand-off page that was committed when it started.
    assert result.scraped_page.url.endswith("/landing"), result.scraped_page.url
    assert [rung for rung, _ in rungs] == ["normal"], f"the first scrape must succeed: {rungs}"
    reload_spy.assert_not_awaited()
    readiness = "wait_for_page_ready" if page_ready_wait else "safe_wait_for_animation_end"
    other = "safe_wait_for_animation_end" if page_ready_wait else "wait_for_page_ready"
    scrape = _first_scrape(order)
    # The wait comes before the frame injection, then the existing flag-selected readiness runs exactly once.
    assert scrape[:4] == ["solver", "navigation_wait", "inject_main", readiness], order
    assert scrape.count(readiness) == 1 and other not in order, order


@requires_chromium
@pytest.mark.asyncio
async def test_a_blank_tab_whose_first_navigation_is_pending_is_scraped_once_it_lands(
    site_browser: tuple[_Site, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new tab still reads about:blank while its first navigation is in flight; that is not a dead blank page."""
    site, base, context = site_browser
    site.landing_header_delay = 10.0
    page = await context.new_page()
    browser_state = RealBrowserState(pw=MagicMock(), browser_context=context, page=page)
    _fast_scrape_failures(monkeypatch)
    agent, kwargs = _agent_and_kwargs(monkeypatch, browser_state)
    reload_spy = _spy_reload(monkeypatch, browser_state)
    rungs = _record_rungs(monkeypatch, agent)
    navigation = browser_state._main_frame_navigations[page]

    await _navigate(page, f"{base}/landing")
    await _until(lambda: site.count("GET", "/landing") == 1 and navigation.is_pending, 10)
    assert page.url == "about:blank"
    result = await agent.build_and_record_step_prompt(**kwargs, persist_artifacts=False)

    assert [rung for rung, _ in rungs] == ["normal"], rungs
    reload_spy.assert_not_awaited()
    assert result.scraped_page.url.endswith("/landing"), result.scraped_page.url
    assert LANDING_MARKER in result.scraped_page.html


@requires_chromium
@pytest.mark.asyncio
@pytest.mark.parametrize("page_ready_wait", [False, True], ids=["animation_wait", "page_ready_wait"])
@pytest.mark.parametrize(
    "reported_after_check", [1, 2], ids=["reported_after_first_rung_check", "reported_after_second_rung_check"]
)
async def test_a_late_reported_navigation_is_still_never_resubmitted(
    site_browser: tuple[_Site, str, Any],
    monkeypatch: pytest.MonkeyPatch,
    page_ready_wait: bool,
    reported_after_check: int,
) -> None:
    """The wait can only act on a navigation the browser has already reported to the observer.

    The hand-off's request events are held back until the ladder's Nth navigation check has passed, as when the
    browser reports them late. A rung that started earlier times out against the navigation; a later wait, at the
    latest the RELOAD rung's, still lets it settle before anything is reloaded.
    """
    real_on_request = real_browser_state_mod._MainFrameNavigation._on_request
    held: list[tuple[Any, Any]] | None = None

    def held_on_request(self: Any, request: Any) -> None:
        if held is None:
            real_on_request(self, request)
        else:
            held.append((self, request))

    monkeypatch.setattr(real_browser_state_mod._MainFrameNavigation, "_on_request", held_on_request)
    site, base, context = site_browser
    site.landing_header_delay = 20.0
    page, browser_state = await _browser_state(context, base)
    _fast_scrape_failures(monkeypatch)
    agent, kwargs = _agent_and_kwargs(monkeypatch, browser_state, page_ready_wait)
    reload_spy = _spy_reload(monkeypatch, browser_state)
    rungs = _record_rungs(monkeypatch, agent)
    checks = 0
    real_check = browser_state.wait_for_main_frame_navigation

    async def check_then_report(*args: Any, **kwargs: Any) -> None:
        nonlocal checks, held
        await real_check(*args, **kwargs)
        checks += 1
        if checks == reported_after_check and held is not None:
            for observer, request in held:
                real_on_request(observer, request)
            held = None

    monkeypatch.setattr(browser_state, "wait_for_main_frame_navigation", check_then_report)

    held = []
    await _start_sign_in(page, base, site)
    assert not browser_state._main_frame_navigations[page].is_pending, "the observer must not have the events yet"
    result = await agent.build_and_record_step_prompt(**kwargs, persist_artifacts=False)

    print(f"LATE_REPORT after_check={reported_after_check} rungs={[rung for rung, _ in rungs]} hits={site.hits}")
    assert site.count("POST", "/assert") == 1, f"sign-in form re-submitted: {site.hits}"
    assert site.count("GET", "/signin") == 1, f"sign-in page reloaded: {site.hits}"
    assert LANDING_MARKER in result.scraped_page.html
    if reported_after_check == 1:
        assert [rung for rung, _ in rungs] == ["normal", "normal"], rungs
        reload_spy.assert_not_awaited()
    else:
        # The RELOAD rung waits for the late-reported navigation, then reloads the settled landing.
        assert [rung for rung, _ in rungs] == ["normal", "normal", "reload"], rungs
        reload_spy.assert_awaited_once_with(degradation=True)
        assert site.count("GET", "/landing") == 2


@requires_chromium
@pytest.mark.asyncio
async def test_a_failing_landing_scrape_still_reaches_the_original_reload(
    site_browser: tuple[_Site, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    site, base, context = site_browser
    site.landing_header_delay = 20.0
    page, browser_state = await _browser_state(context, base)
    _fast_scrape_failures(monkeypatch)
    agent, kwargs = _agent_and_kwargs(monkeypatch, browser_state)
    real_prepare = scraper_mod._prepare_scrape_frame
    failures_after_landing = 0

    async def landing_unusable_twice(*args: Any, **kwargs: Any) -> Any:
        nonlocal failures_after_landing
        prepared = await real_prepare(*args, **kwargs)
        # Both NORMAL rungs fail after the wait has settled on the landing; the RELOAD rung's scrape is real.
        if site.count("SENT", "/landing") and failures_after_landing < 2:
            failures_after_landing += 1
            raise RuntimeError("landing not usable yet")
        return prepared

    monkeypatch.setattr(scraper_mod, "_prepare_scrape_frame", landing_unusable_twice)
    reload_spy = _spy_reload(monkeypatch, browser_state)

    await _start_sign_in(page, base, site, browser_state)
    result = await agent.build_and_record_step_prompt(**kwargs, persist_artifacts=False)

    assert failures_after_landing == 2, "both NORMAL rungs should have scraped the settled landing"
    reload_spy.assert_awaited_once_with(degradation=True)
    assert site.count("POST", "/assert") == 1, f"sign-in form re-submitted: {site.hits}"
    assert site.count("GET", "/landing") == 2, "the original reload must reload the settled landing"
    assert LANDING_MARKER in result.scraped_page.html


@requires_chromium
@pytest.mark.asyncio
async def test_a_landing_that_never_answers_gets_the_original_reload_within_one_ladder_budget(
    site_browser: tuple[_Site, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    site, base, context = site_browser
    site.landing_never_answers = True
    page, browser_state = await _browser_state(context, base)
    _fast_scrape_failures(monkeypatch)
    # Short enough to keep the test fast; the budget counts from the navigation's start.
    monkeypatch.setattr(settings, "BROWSER_LOADING_TIMEOUT_MS", 15_000)
    agent, kwargs = _agent_and_kwargs(monkeypatch, browser_state)
    rungs = _record_rungs(monkeypatch, agent)
    reload_spy = _spy_reload(monkeypatch, browser_state)

    await _start_sign_in(page, base, site, browser_state)
    navigation_started = time.monotonic()
    with pytest.raises(Exception):
        await agent.build_and_record_step_prompt(**kwargs, persist_artifacts=False)

    assert [rung for rung, _ in rungs] == ["normal", "normal", "reload"], rungs
    # Only the attempt is asserted: Chromium may abort a reload issued over a navigation that never answers before
    # its request leaves the browser, so whether the server saw a second sign-in GET is reported, not required.
    reload_spy.assert_awaited_once_with(degradation=True)
    print(f"NEVER_ANSWERS signin_gets={site.count('GET', '/signin')} hits={site.hits}")
    assert rungs[1][1] - navigation_started >= 15 - 1, "the first scrape must wait out the loading budget"
    assert rungs[2][1] - rungs[1][1] < 15, "a later rung must not wait out another budget"


@requires_chromium
@pytest.mark.asyncio
async def test_cancelling_the_ladder_during_the_wait_cancels_without_reloading(
    site_browser: tuple[_Site, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    site, base, context = site_browser
    site.landing_never_answers = True
    page, browser_state = await _browser_state(context, base)
    _fast_scrape_failures(monkeypatch)
    agent, kwargs = _agent_and_kwargs(monkeypatch, browser_state)
    reload_spy = _spy_reload(monkeypatch, browser_state)
    waiting = asyncio.Event()
    real_wait = real_browser_state_mod._MainFrameNavigation.wait

    async def observed_wait(self: Any, deadline: float) -> None:
        waiting.set()
        await real_wait(self, deadline)

    monkeypatch.setattr(real_browser_state_mod._MainFrameNavigation, "wait", observed_wait)

    await _start_sign_in(page, base, site, browser_state)
    ladder = asyncio.create_task(agent.build_and_record_step_prompt(**kwargs, persist_artifacts=False))
    await asyncio.wait_for(waiting.wait(), 30)
    ladder.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(ladder, 5)
    reload_spy.assert_not_awaited()
    assert site.count("GET", "/signin") == 1


@requires_chromium
@pytest.mark.asyncio
async def test_a_scrape_outside_the_ladder_does_not_wait_for_the_navigation(
    site_browser: tuple[_Site, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refreshes and other non-ladder scrapes pass no deadline, so they keep today's behavior."""
    site, base, context = site_browser
    site.landing_never_answers = True
    page, browser_state = await _browser_state(context, base)
    _fast_scrape_failures(monkeypatch)
    _agent_and_kwargs(monkeypatch, browser_state)
    order = _record_scrape_order(monkeypatch, browser_state)

    await _start_sign_in(page, base, site, browser_state)
    started = time.monotonic()
    with pytest.raises(Exception):
        await browser_state.scrape_website(
            url=f"{base}/signin", cleanup_element_tree=AsyncMock(side_effect=lambda *a: a[2])
        )

    assert "navigation_wait" not in order, order
    assert time.monotonic() - started < 15, "a non-ladder scrape must not wait for the navigation"
    assert site.count("POST", "/assert") == 1


@requires_chromium
@pytest.mark.asyncio
async def test_ladder_reloads_immediately_when_nothing_is_pending(
    site_browser: tuple[_Site, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injected NORMAL failures on an idle page: the RELOAD rung keeps today's behavior and adds no wait."""
    site, base, context = site_browser
    page, browser_state = await _browser_state(context, base)
    agent, kwargs = _agent_and_kwargs(monkeypatch, browser_state)
    real_scrape = scraper_mod.scrape_web_unsafe
    calls = 0

    async def fail_twice(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("renderer wedged")
        return await real_scrape(*args, **kwargs)

    monkeypatch.setattr(scraper_mod, "scrape_web_unsafe", fail_twice)
    reload_called_at: list[float] = []
    real_reload = browser_state.reload_page

    async def timed_reload(**kwargs: Any) -> None:
        reload_called_at.append(time.monotonic())
        await real_reload(**kwargs)

    monkeypatch.setattr(browser_state, "reload_page", timed_reload)
    rungs = _record_rungs(monkeypatch, agent)

    result = await agent.build_and_record_step_prompt(**kwargs, persist_artifacts=False)

    assert [rung for rung, _ in rungs] == ["normal", "normal", "reload"], rungs
    assert len(reload_called_at) == 1
    assert reload_called_at[0] - rungs[-1][1] < 1.0, "no wait may precede the reload when nothing is pending"
    assert "start" in result.scraped_page.html


@requires_chromium
@pytest.mark.asyncio
async def test_a_wedged_renderer_gets_the_original_reload_without_waiting(
    site_browser: tuple[_Site, str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy-looping page has no pending navigation, so the RELOAD rung reloads at once, as before.

    The reload does not recover this wedge in Chromium; only its invocation and timing are asserted.
    """
    site, base, context = site_browser
    page, browser_state = await _browser_state(context, base, "/wedge")
    await asyncio.sleep(0.6)
    _fast_scrape_failures(monkeypatch)
    monkeypatch.setattr(settings, "BROWSER_LOADING_TIMEOUT_MS", 3_000)
    agent, kwargs = _agent_and_kwargs(monkeypatch, browser_state)
    reload_called_at: list[float] = []
    real_reload = browser_state.reload_page

    async def timed_reload(**kwargs: Any) -> None:
        reload_called_at.append(time.monotonic())
        await real_reload(**kwargs)

    monkeypatch.setattr(browser_state, "reload_page", timed_reload)
    rungs = _record_rungs(monkeypatch, agent)

    started = time.monotonic()
    try:
        await agent.build_and_record_step_prompt(**kwargs, persist_artifacts=False)
        outcome = "recovered"
    except Exception as e:
        outcome = type(e).__name__

    assert len(reload_called_at) == 1, "the RELOAD rung must invoke the original reload exactly once"
    reload_delay = reload_called_at[0] - rungs[-1][1]
    print(
        f"WEDGE_RESCUE outcome={outcome} rungs={len(rungs)} "
        f"reload_after_rung_entry_s={reload_delay:.3f} total_s={time.monotonic() - started:.3f}"
    )
    assert reload_delay < 1.0, "no wait may precede the reload when nothing is pending"


@requires_chromium
@pytest.mark.asyncio
@pytest.mark.parametrize("page_ready_wait", [False, True], ids=["animation_wait", "page_ready_wait"])
@pytest.mark.parametrize("path", ["/start", "/spinner-gone", "/spinner-stuck", "/iframe-loading"])
async def test_pages_with_nothing_pending_scrape_on_the_first_rung(
    site_browser: tuple[_Site, str, Any], monkeypatch: pytest.MonkeyPatch, path: str, page_ready_wait: bool
) -> None:
    site, base, context = site_browser
    page, browser_state = await _browser_state(context, base, path)
    agent, kwargs = _agent_and_kwargs(monkeypatch, browser_state, page_ready_wait)
    reload_spy = _spy_reload(monkeypatch, browser_state)
    order = _record_scrape_order(monkeypatch, browser_state)

    started = time.monotonic()
    result = await agent.build_and_record_step_prompt(**kwargs, persist_artifacts=False)
    print(f"FIRST_RUNG path={path} page_ready_wait={page_ready_wait} elapsed_s={time.monotonic() - started:.3f}")

    reload_spy.assert_not_awaited()
    assert "start" in result.scraped_page.html
    readiness = "wait_for_page_ready" if page_ready_wait else "safe_wait_for_animation_end"
    scrape = _first_scrape(order)
    # The challenge-solver wait keeps its place ahead of the scraper; only the main page waits on navigation.
    assert scrape[:4] == ["solver", "navigation_wait", "inject_main", readiness], order
    assert scrape.count("navigation_wait") == 1, order


def _navigation(browser_state: RealBrowserState, page: Any) -> Any:
    navigation = browser_state._main_frame_navigations.get(page)
    assert navigation is not None, "the page should be observed from creation"
    return navigation


async def _navigate(page: Any, url: str) -> None:
    try:
        await asyncio.wait_for(page.evaluate(f"location.href = '{url}'"), 2)
    except Exception:
        pass


async def _until(condition: Any, seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


def _settled(navigation: Any) -> bool:
    return not navigation.is_pending and not navigation.closed


@requires_chromium
@pytest.mark.asyncio
async def test_a_307_redirect_keeps_the_navigation_pending_until_the_final_response(
    site_browser: tuple[_Site, str, Any],
) -> None:
    site, base, context = site_browser
    page, browser_state = await _browser_state(context, base)
    navigation = _navigation(browser_state, page)

    await _navigate(page, f"{base}/moved")
    await _until(lambda: site.count("GET", "/slow") == 1)
    assert navigation.is_pending, "a redirect hop must keep the navigation pending"

    await navigation.wait(time.monotonic() + 10)
    assert _settled(navigation)
    # Settling means the response arrived; the commit follows it.
    await page.wait_for_url("**/slow", timeout=5_000)


@requires_chromium
@pytest.mark.asyncio
async def test_a_replaced_navigations_late_failure_does_not_settle_its_replacement(
    site_browser: tuple[_Site, str, Any],
) -> None:
    site, base, context = site_browser
    page, browser_state = await _browser_state(context, base)
    navigation = _navigation(browser_state, page)

    await _navigate(page, f"{base}/hang")
    await _until(lambda: site.count("GET", "/hang") == 1)
    replaced = asyncio.Event()
    page.on("requestfailed", lambda request: replaced.set() if "/hang" in request.url else None)
    goto = asyncio.create_task(page.goto(f"{base}/slow", timeout=10_000))
    await asyncio.wait_for(replaced.wait(), 5)
    await _until(lambda: site.count("GET", "/slow") == 1)
    # The browser's request event can trail the server seeing the request.
    await _until(lambda: navigation.is_pending, 3)

    await navigation.wait(time.monotonic() + 10)
    assert _settled(navigation)
    await goto
    assert page.url.endswith("/slow")


@requires_chromium
@pytest.mark.asyncio
async def test_a_same_document_url_change_does_not_settle_the_navigation(
    site_browser: tuple[_Site, str, Any],
) -> None:
    site, base, context = site_browser
    page, browser_state = await _browser_state(context, base)
    navigation = _navigation(browser_state, page)
    same_document_change = asyncio.Event()
    page.on("framenavigated", lambda frame: same_document_change.set() if "#moved" in frame.url else None)

    await _navigate(page, f"{base}/pushstate")
    await asyncio.wait_for(same_document_change.wait(), 5)

    assert site.count("GET", "/hang") == 1
    assert navigation.is_pending


@requires_chromium
@pytest.mark.asyncio
async def test_iframe_navigations_are_ignored_and_downloads_and_empty_responses_settle(
    site_browser: tuple[_Site, str, Any],
) -> None:
    site, base, context = site_browser
    page, browser_state = await _browser_state(context, base)
    navigation = _navigation(browser_state, page)

    await page.goto(f"{base}/iframe-loading", wait_until="commit")
    await _until(lambda: site.count("GET", "/hang") == 1)
    assert not navigation.is_pending, "a pending iframe navigation is not the page's navigation"

    for path in ("/file", "/empty"):
        await page.goto(f"{base}/start")
        await _navigate(page, f"{base}{path}")
        await navigation.wait(time.monotonic() + 5)
        assert _settled(navigation), path


@requires_chromium
@pytest.mark.asyncio
@pytest.mark.parametrize("ends", ["page_close", "browser_close"])
async def test_closing_the_page_or_browser_wakes_a_waiter(site_browser: tuple[_Site, str, Any], ends: str) -> None:
    site, base, context = site_browser
    page, browser_state = await _browser_state(context, base)
    navigation = _navigation(browser_state, page)
    await _navigate(page, f"{base}/hang")
    await _until(lambda: site.count("GET", "/hang") == 1)

    waiter = asyncio.create_task(navigation.wait(time.monotonic() + 30))
    await asyncio.sleep(0.2)
    if ends == "page_close":
        await page.close()
    else:
        await context.browser.close()

    await asyncio.wait_for(waiter, 5)
    assert not navigation.is_pending


class _FakeFrame:
    def __init__(self) -> None:
        self._impl_obj = SimpleNamespace(_event_emitter=EventEmitter())

    def commit(self, request: Any) -> None:
        self._impl_obj._event_emitter.emit("navigated", {"newDocument": {"request": request._impl_obj._channel}})


@dataclass
class _FakeRequest:
    frame: Any
    redirected_from: Any = None
    navigation: bool = True
    _impl_obj: Any = field(default_factory=lambda: SimpleNamespace(_channel=object()), repr=False)

    def is_navigation_request(self) -> bool:
        return self.navigation


@dataclass
class _FakeResponse:
    request: Any
    status: int


class _FakePage:
    def __init__(self) -> None:
        self.main_frame = _FakeFrame()
        self.listeners: dict[str, list[Any]] = {}
        self.closed = False

    def on(self, event: str, handler: Any) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        self.listeners[event].remove(handler)

    def emit(self, event: str, payload: Any) -> None:
        for handler in list(self.listeners.get(event, [])):
            handler(payload)

    def is_closed(self) -> bool:
        return self.closed

    def listener_count(self) -> int:
        return sum(len(handlers) for handlers in self.listeners.values()) + len(
            self.main_frame._impl_obj._event_emitter.listeners("navigated")
        )


def _observed_page(state: RealBrowserState | None = None) -> tuple[_FakePage, RealBrowserState, Any]:
    state = state or RealBrowserState(pw=AsyncMock(), browser_context=None)
    page = _FakePage()
    state._observe_main_frame_navigation(page)  # type: ignore[arg-type]
    return page, state, state._main_frame_navigations[page]  # type: ignore[index]


@pytest.mark.asyncio
async def test_the_navigation_wait_ends_at_the_earlier_of_the_chain_budget_and_the_ladder_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, state, _ = _observed_page()
    monkeypatch.setattr(settings, "BROWSER_LOADING_TIMEOUT_MS", 1_000)
    page.emit("request", _FakeRequest(frame=page.main_frame))

    started = time.monotonic()
    await state.wait_for_main_frame_navigation(page, started + 0.3)  # type: ignore[arg-type]
    assert 0.25 <= time.monotonic() - started < 0.8, "the ladder deadline must cut the wait short"

    await state.wait_for_main_frame_navigation(page, started + 30)  # type: ignore[arg-type]
    assert 0.9 <= time.monotonic() - started < 1.5, "the chain budget must cap a later ladder deadline"


@pytest.mark.asyncio
async def test_a_replacement_navigation_cannot_extend_the_ladder_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    page, state, _ = _observed_page()
    first = _FakeRequest(frame=page.main_frame)
    page.emit("request", first)
    started = time.monotonic()
    deadline = started + 0.6
    loop = asyncio.get_running_loop()
    loop.call_later(0.2, page.emit, "response", _FakeResponse(request=first, status=200))
    loop.call_later(0.2, page.main_frame.commit, first)
    loop.call_later(0.25, page.emit, "request", _FakeRequest(frame=page.main_frame))

    await state.wait_for_main_frame_navigation(page, deadline)  # type: ignore[arg-type]
    assert time.monotonic() - started < 0.55, "the wait ends when the navigation it waited for settles"
    await asyncio.sleep(0.1)
    await state.wait_for_main_frame_navigation(page, deadline)  # type: ignore[arg-type]
    assert 0.55 <= time.monotonic() - started < 1.1, "the replacement is waited for only until the ladder deadline"


@pytest.mark.asyncio
async def test_nothing_pending_or_an_unobserved_page_returns_at_once() -> None:
    page, state, _ = _observed_page()
    started = time.monotonic()
    await state.wait_for_main_frame_navigation(page, started + 30)  # type: ignore[arg-type]
    await state.wait_for_main_frame_navigation(_FakePage(), started + 30)  # type: ignore[arg-type]
    assert time.monotonic() - started < 0.05


@pytest.mark.asyncio
async def test_crash_wakes_waiters_and_detaches() -> None:
    page, _, navigation = _observed_page()
    page.emit("request", _FakeRequest(frame=page.main_frame))
    waiter = asyncio.create_task(navigation.wait(time.monotonic() + 30))
    await asyncio.sleep(0)

    page.emit("crash", page)

    await asyncio.wait_for(waiter, 1)
    assert navigation.closed
    assert page.listener_count() == 0


@pytest.mark.asyncio
async def test_a_superseded_requests_late_failure_does_not_settle_its_replacement() -> None:
    page, _, navigation = _observed_page()
    superseded = _FakeRequest(frame=page.main_frame)
    replacement = _FakeRequest(frame=page.main_frame)
    page.emit("request", superseded)
    page.emit("request", replacement)

    page.emit("requestfailed", superseded)
    assert navigation.is_pending
    page.emit("requestfailed", replacement)
    assert not navigation.is_pending


@pytest.mark.asyncio
async def test_concurrent_waiters_share_the_navigation_and_cancelling_one_leaves_the_other() -> None:
    page, _, navigation = _observed_page()
    request = _FakeRequest(frame=page.main_frame)
    page.emit("request", request)
    first = asyncio.create_task(navigation.wait(time.monotonic() + 30))
    second = asyncio.create_task(navigation.wait(time.monotonic() + 30))
    await asyncio.sleep(0)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert navigation.is_pending and not second.done()

    page.emit("response", _FakeResponse(request=request, status=200))
    assert not second.done(), "headers alone must not release the other waiter"
    page.main_frame.commit(request)
    await asyncio.wait_for(second, 1)
    assert _settled(navigation)


@pytest.mark.parametrize("status", [204, 205, 304])
def test_redirects_keep_the_chain_start_and_no_document_responses_settle(status: int) -> None:
    page, _, navigation = _observed_page()
    first = _FakeRequest(frame=page.main_frame)
    page.emit("request", first)
    started_at = navigation.started_at
    for redirect_status in (301, 302, 303, 307, 308):
        hop = _FakeRequest(frame=page.main_frame, redirected_from=first)
        page.emit("response", _FakeResponse(request=first, status=redirect_status))
        page.emit("request", hop)
        assert navigation.is_pending, f"{redirect_status} is a redirect and must keep the navigation pending"
        assert navigation.started_at == started_at, "a redirect hop must not restart the budget"
        first = hop

    page.emit("response", _FakeResponse(request=first, status=status))
    assert not navigation.is_pending, f"{status} leaves the old document in place, so it must settle"


def test_response_headers_and_same_document_events_do_not_settle_a_pending_document() -> None:
    page, _, navigation = _observed_page()
    request = _FakeRequest(frame=page.main_frame)
    page.emit("request", request)
    page.emit("response", _FakeResponse(request=request, status=200))
    assert navigation.is_pending, "response headers do not mean the document committed"

    page.main_frame._impl_obj._event_emitter.emit("navigated", {"url": "https://example.com/#moved"})
    assert navigation.is_pending, "a same-document URL change cannot commit the pending document"
    replacement = _FakeRequest(frame=page.main_frame)
    page.emit("request", replacement)
    page.main_frame.commit(request)
    assert navigation.is_pending, "a superseded request's commit must not settle its replacement"
    page.main_frame.commit(replacement)
    assert _settled(navigation)


def test_iframe_and_unknown_frame_ids_are_not_the_main_frame_navigation() -> None:
    page, _, navigation = _observed_page()

    page.emit("request", _FakeRequest(frame=_FakeFrame()))
    assert not navigation.is_pending

    @dataclass
    class _CdpFrame:
        frame_id: str | None

    @dataclass
    class _CdpRequest(_FakeRequest):
        frame_id: str | None = None

    page.main_frame = _CdpFrame(frame_id="main")  # type: ignore[assignment]
    page.emit("request", _CdpRequest(frame=page.main_frame, frame_id="child"))
    assert not navigation.is_pending, "a frame id fallback to the main frame must not count"
    page.emit("request", _CdpRequest(frame=page.main_frame, frame_id="main"))
    assert navigation.is_pending


def test_raw_cdp_document_commit_ignores_iframes_and_a_superseded_url() -> None:
    page = _FakePage()
    page.main_frame = SimpleNamespace(frame_id="main", url="https://example.com/old")
    navigation = real_browser_state_mod._MainFrameNavigation(page)  # type: ignore[arg-type]
    request = SimpleNamespace(
        frame=page.main_frame,
        frame_id="main",
        url="https://example.com/landing",
        redirected_from=None,
        is_navigation_request=lambda: True,
    )
    page.emit("request", request)
    page.emit("response", _FakeResponse(request=request, status=200))
    page.emit("framenavigated", SimpleNamespace(frame_id="child", url=request.url))
    assert navigation.is_pending
    page.emit("framenavigated", page.main_frame)
    assert navigation.is_pending
    page.main_frame.url = f"{request.url}#ready"
    page.emit("framenavigated", page.main_frame)
    assert _settled(navigation)
    navigation.detach()
    assert not any(page.listeners.values())


@pytest.mark.parametrize("gone_event", ["close", "crash"])
def test_a_closed_or_crashed_page_is_released_by_the_browser_state(gone_event: str) -> None:
    page, state, _ = _observed_page()
    page.emit("request", _FakeRequest(frame=page.main_frame))
    page.emit(gone_event, page)
    released = weakref.ref(page)

    del page
    gc.collect()

    assert released() is None, "the browser state must not keep a closed page alive"
    assert len(state._main_frame_navigations) == 0


@pytest.mark.asyncio
async def test_a_keep_alive_close_keeps_observing_the_reused_page() -> None:
    """A state kept for reuse (persistent session, shared browser) must keep guarding its pages."""
    page, state, navigation = _observed_page()

    await state.close(close_browser_on_completion=False, release_driver=False)

    assert state._main_frame_navigations.get(page) is navigation  # type: ignore[call-overload]
    page.emit("request", _FakeRequest(frame=page.main_frame))
    assert navigation.is_pending, "a reused page's navigation must still be observed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "close_kwargs",
    [
        {"close_browser_on_completion": True, "release_driver": False},
        {"close_browser_on_completion": False, "release_driver": True},
    ],
)
async def test_a_terminal_close_or_driver_release_detaches_and_wakes_waiters(close_kwargs: dict[str, bool]) -> None:
    page, state, navigation = _observed_page()
    page.emit("request", _FakeRequest(frame=page.main_frame))
    waiter = asyncio.create_task(navigation.wait(time.monotonic() + 30))
    await asyncio.sleep(0)

    await state.close(**close_kwargs)

    await asyncio.wait_for(waiter, 1)
    assert navigation.closed
    assert state._main_frame_navigations.get(page) is None  # type: ignore[call-overload]
    assert page.listener_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("release_driver", "kept"), [(False, True), (None, False)])
async def test_an_attached_external_browser_releases_observation_only_with_its_driver(
    release_driver: bool | None, kept: bool
) -> None:
    state = RealBrowserState(pw=AsyncMock(), browser_context=None)
    state.external_browser = True
    page, _, _ = _observed_page(state)

    await state.close(close_browser_on_completion=False, release_driver=release_driver)

    assert (state._main_frame_navigations.get(page) is not None) is kept  # type: ignore[call-overload]


@requires_chromium
@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["playwright", "patchright"])
async def test_a_closed_context_reports_stopped_while_its_browser_stays_connected(driver: str) -> None:
    async_api = pytest.importorskip(f"{driver}.async_api")
    async with async_api.async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-proxy-server"])
        try:
            sibling = await browser.new_context()
            closed = await browser.new_context()
            await closed.new_page()
            await closed.close()

            assert browser.is_connected()
            # The exact reason pins the driver's close flag; a disconnect or connection latch would report another.
            assert real_browser_state_mod.browser_context_stopped_reason(closed) == "browser_context_close_called"
            assert real_browser_state_mod.browser_context_stopped_reason(sibling) is None
        finally:
            await browser.close()

"""URL matching and dispatch order for network routing.

Two production security guards in Skyvern Cloud depend on the exact semantics here, and both fail *open* if the
details are wrong, without raising anything. So the details are pinned rather than described:

- Playwright's glob is not Chrome's. `*` does not cross `/`, `**` does, and `?` is a literal, not a
  wildcard. Matching happens in Python for that reason; Chrome's own matcher cannot express it.
- Handlers dispatch last-registered-first, and a handler that calls `fallback()` hands down to the
  next one. The cloud agent functions register the candidate guard last specifically so it runs
  first, and the whole passthrough mechanism in the egress policy exists because `continue_()`
  short-circuits the chain while `fallback()` does not.
"""

from __future__ import annotations

import asyncio
import base64
import re

import pytest
import pytest_asyncio

from skyvern.webeye.skycdp.connection import CdpConnection, CdpSession, TargetInfo
from skyvern.webeye.skycdp.errors import CdpError
from skyvern.webeye.skycdp.facade.network import FetchRouter, Request, Route, dispatch
from skyvern.webeye.skycdp.facade.routing import RouteTable, glob_to_regex, url_matches
from skyvern.webeye.skycdp.transport import CdpTransport
from tests.unit.test_skycdp_transport import FakeSocket


@pytest.mark.parametrize(
    ("pattern", "url", "expected"),
    [
        ("**/*", "https://example.invalid/a/b", True),
        ("https://*.example.invalid/**", "https://api.example.invalid/x/y", True),
        ("https://*.example.invalid/**", "https://example.invalid/x", False),
        # A single star must not cross a path separator; a double star must.
        ("https://host.invalid/*", "https://host.invalid/one", True),
        ("https://host.invalid/*", "https://host.invalid/one/two", False),
        ("https://host.invalid/**", "https://host.invalid/one/two", True),
        # Brace alternation.
        ("https://host.invalid/{a,b}", "https://host.invalid/b", True),
        ("https://host.invalid/{a,b}", "https://host.invalid/c", False),
        # A question mark is a literal, not a single-character wildcard.
        ("https://host.invalid/a?b", "https://host.invalid/a?b", True),
        ("https://host.invalid/a?b", "https://host.invalid/axb", False),
        # Anchored at both ends.
        ("https://host.invalid/a", "https://host.invalid/ab", False),
        ("http://fixture.invalid", "http://fixture.invalid/", True),
        ("http://LOCALHOST:8080/api", "http://localhost:8080/api", True),
        ("HTTP://localhost:8080/api", "http://localhost:8080/api", True),
        ("http://LOCALHOST:8080", "http://localhost:8080/", True),
        ("https://User:Pass@HOST.invalid/a?X=Y", "https://User:Pass@host.invalid/a?X=Y", True),
        ("ws://HOST.invalid/socket", "ws://host.invalid/socket", True),
        ("WSS://HOST.invalid/socket", "wss://host.invalid/socket", True),
        ("http://fixture.invalid?x=1", "http://fixture.invalid/?x=1", True),
        ("http://fixture.invalid/foo**/bar", "http://fixture.invalid/foobar", False),
        ("http://fixture.invalid/foo**/bar", "http://fixture.invalid/foo/x/bar", True),
        ("a/**/b", "a/b", True),
    ],
)
def test_playwright_glob_semantics(pattern: str, url: str, expected: bool) -> None:
    assert bool(re.match(glob_to_regex(pattern), url)) is expected


def test_handlers_run_last_registered_first() -> None:
    """LIFO. The cloud agent functions rely on it to keep the candidate guard ahead of the blocker."""
    table = RouteTable()
    order: list[str] = []

    def first(route: object) -> None:
        order.append("first")

    def second(route: object) -> None:
        order.append("second")

    table.add("**/*", first)
    table.add("**/*", second)

    assert [entry.handler for entry in table.matching("https://h.invalid/x")] == [second, first]


@pytest.mark.parametrize(
    ("original", "remaining", "url", "specific"),
    [
        ("https://other.invalid/**", "**/*", "https://other.invalid/x", False),
        ("http://fixture.invalid", "http://fixture.invalid/", "http://fixture.invalid/", True),
        ("HTTP://FIXTURE.invalid", "HTTP://FIXTURE.invalid/", "http://fixture.invalid/", True),
    ],
)
def test_unroute_removes_only_the_named_pattern(original, remaining, url, specific) -> None:
    table = RouteTable()

    def handler(route: object) -> None:
        return None

    table.add(remaining, handler)
    table.add(original, handler)
    assert len(list(table.matching(url))) == 2
    table.remove(original, handler if specific else None)
    assert [entry.matcher for entry in table.matching(url)] == [remaining]


def test_unroute_without_a_handler_removes_every_handler_for_the_pattern() -> None:
    table = RouteTable()

    def one(route: object) -> None:
        return None

    def two(route: object) -> None:
        return None

    table.add("**/*", one)
    table.add("**/*", two)
    table.remove("**/*")
    assert list(table.matching("https://h.invalid/x")) == []


def test_unroute_with_a_handler_removes_only_that_handler() -> None:
    table = RouteTable()

    def one(route: object) -> None:
        return None

    def two(route: object) -> None:
        return None

    table.add("**/*", one)
    table.add("**/*", two)
    table.remove("**/*", one)

    assert [entry.handler for entry in table.matching("https://h.invalid/x")] == [two]


@pytest_asyncio.fixture
async def paused_session():
    socket = FakeSocket()
    transport = CdpTransport(socket, default_timeout=0.1)
    await transport.start()
    connection = CdpConnection(transport)
    session = CdpSession(connection, "routing", TargetInfo("target", "page", "about:blank"))
    try:
        yield session, socket
    finally:
        await transport.close()


def paused_request() -> dict:
    return {"requestId": "paused", "request": {"url": "https://h.invalid/original", "method": "POST"}}


async def _case_fallback_matches_original_url_across_page_and_context_and_keeps_bytes(paused_session) -> None:
    session, socket = paused_session
    page, context = RouteTable(), RouteTable()
    seen: list[tuple[str, str | None]] = []

    async def rewrite(route: Route) -> None:
        await route.fallback(url="https://h.invalid/rewritten", headers={"x-test": "kept"}, post_data=b"\x00\xff")

    async def guard(route: Route, request: Request) -> None:
        seen.append((request.url, request.headers.get("x-test")))
        assert request.post_data_buffer == b"\x00\xff"
        await route.continue_(method="PUT")

    page.add("**/original", rewrite)
    context.add("**/original", guard)
    pending = asyncio.create_task(dispatch(session, [page, context], paused_request()))
    await socket.wait_for_sent(1)
    socket.push({"id": socket.sent[0]["id"], "result": {}})
    await pending
    assert seen == [("https://h.invalid/rewritten", "kept")]
    assert socket.sent[0]["params"] == {
        "requestId": "paused",
        "url": "https://h.invalid/rewritten",
        "method": "PUT",
        "headers": [{"name": "x-test", "value": "kept"}],
        "postData": base64.b64encode(b"\x00\xff").decode(),
    }


async def _case_terminal_command_requires_ack_and_survives_caller_cancellation(paused_session) -> None:
    session, socket = paused_session
    route = Route(session, "paused", Request(paused_request()))
    pending = asyncio.create_task(route.continue_())
    await socket.wait_for_sent(1)
    assert not route.settled
    pending.cancel()
    with pytest.raises(CdpError):
        await route.abort()
    socket.push({"id": socket.sent[0]["id"], "result": {}})
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert route.settled
    assert len(socket.sent) == 1


async def _case_rejected_continue_is_failed_closed_instead_of_reported_as_settled(
    paused_session, code, message
) -> None:
    session, socket = paused_session
    table = RouteTable()

    async def forward(route: Route) -> None:
        await route.continue_()

    table.add("**/*", forward)
    pending = asyncio.create_task(dispatch(session, [table], paused_request()))
    await socket.wait_for_sent(1)
    socket.push({"id": socket.sent[0]["id"], "error": {"code": code, "message": message}})
    await socket.wait_for_sent(2)
    assert socket.sent[1]["method"] == "Fetch.failRequest"
    socket.push({"id": socket.sent[1]["id"], "result": {}})
    await pending


async def _case_cancelling_a_handler_fails_its_paused_request(paused_session) -> None:
    session, socket = paused_session
    table = RouteTable()
    entered = asyncio.Event()

    async def waiting(route: Route) -> None:
        entered.set()
        await asyncio.Event().wait()

    table.add("**/*", waiting)
    pending = asyncio.create_task(dispatch(session, [table], paused_request()))
    await entered.wait()
    pending.cancel()
    await socket.wait_for_sent(1)
    assert socket.sent[0]["method"] == "Fetch.failRequest"
    socket.push({"id": socket.sent[0]["id"], "result": {}})
    with pytest.raises(asyncio.CancelledError):
        await pending


async def _case_dispatch_errors_cannot_bypass_the_guard(paused_session, failure: str) -> None:
    session, socket = paused_session
    table = RouteTable()

    def matcher(url: str) -> bool:
        raise ValueError("broken matcher")

    table.add(matcher if failure == "matcher" else "**/*", lambda route: None)
    pending = asyncio.create_task(dispatch(session, [table], paused_request()))
    await socket.wait_for_sent(1)
    socket.push({"id": socket.sent[0]["id"], "result": {}})
    await pending
    assert socket.sent[0]["method"] == "Fetch.failRequest"


async def enable_router(
    session: CdpSession, socket: FakeSocket, table: RouteTable, failure: asyncio.Event
) -> FetchRouter:
    router = FetchRouter(table)

    async def close_target() -> None:
        failure.set()

    enabling = asyncio.create_task(router.enable(session, RouteTable(), close_target))
    await socket.wait_for_sent(1)
    socket.push({"id": socket.sent[0]["id"], "result": {}})
    await enabling
    return router


async def _case_stale_interception_finishes_without_closing_a_healthy_target(
    paused_session, terminal: str, outcomes: tuple[str | None, ...], closes_target: bool
) -> None:
    session, socket = paused_session
    table, failed = RouteTable(), asyncio.Event()

    async def handler(route: Route) -> None:
        if route.request.url.endswith("healthy"):
            await route.fulfill(body="still guarded")
        else:
            await getattr(route, terminal)()

    table.add("**/*", handler)
    router = await enable_router(session, socket, table, failed)
    owner = router._sessions[session]
    try:
        socket.push({"method": "Fetch.requestPaused", "sessionId": "routing", "params": paused_request()})
        for index, message in enumerate(outcomes, start=2):
            await socket.wait_for_sent(index)
            frame = socket.sent[-1]
            if index > 2:
                assert frame["method"] == "Fetch.failRequest"
            reply = {"result": {}} if message is None else {"error": {"code": -32602, "message": message}}
            socket.push({"id": frame["id"], **reply})
        await asyncio.wait_for(asyncio.gather(*owner._tasks.values(), return_exceptions=True), 1)
        if owner._failure is not None:
            await owner._failure
        assert failed.is_set() is closes_target
        assert not owner._tasks
        assert len(socket.sent) == 1 + len(outcomes)
        if not closes_target:
            socket.push(
                {
                    "method": "Fetch.requestPaused",
                    "sessionId": "routing",
                    "params": {"requestId": "healthy", "request": {"url": "https://h.invalid/healthy"}},
                }
            )
            await socket.wait_for_sent(len(outcomes) + 2)
            assert socket.sent[-1]["method"] == "Fetch.fulfillRequest"
            socket.push({"id": socket.sent[-1]["id"], "result": {}})
            await asyncio.wait_for(asyncio.gather(*owner._tasks.values()), 1)
            assert not owner._tasks and not failed.is_set()
        assert not session._connection.transport._pending
    finally:
        await router.close(session)


async def _case_missing_terminal_ack_closes_target_without_replaying_or_disabling(paused_session) -> None:
    session, socket = paused_session
    table, failed = RouteTable(), asyncio.Event()

    async def forward(route: Route) -> None:
        await route.continue_()

    table.add("**/*", forward)
    router = await enable_router(session, socket, table, failed)
    socket.push({"method": "Fetch.requestPaused", "sessionId": "routing", "params": paused_request()})
    await asyncio.wait_for(failed.wait(), 1)
    await router.close(session)
    assert [frame["method"] for frame in socket.sent] == ["Fetch.enable", "Fetch.continueRequest"]


async def _case_owner_close_cancels_handlers_without_releasing_other_paused_requests(paused_session) -> None:
    session, socket = paused_session
    table, failed, entered, cancelled = RouteTable(), asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def waiting(route: Route) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    table.add("**/*", waiting)
    router = await enable_router(session, socket, table, failed)
    socket.push({"method": "Fetch.requestPaused", "sessionId": "routing", "params": paused_request()})
    await entered.wait()
    closing = asyncio.create_task(router.close(session))
    await socket.wait_for_sent(2)
    assert cancelled.is_set()
    assert socket.sent[1]["method"] == "Fetch.failRequest"
    socket.push({"id": socket.sent[1]["id"], "result": {}})
    await asyncio.wait_for(closing, 1)
    assert [frame["method"] for frame in socket.sent] == ["Fetch.enable", "Fetch.failRequest"]
    assert not failed.is_set()
    assert not any(task.get_name() == "cdp-fetch-dispatch" for task in asyncio.all_tasks())


async def _case_removal_does_not_resume_a_request_before_its_handler_ack(paused_session) -> None:
    session, socket = paused_session
    table, failed, entered, release = RouteTable(), asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def waiting(route: Route) -> None:
        entered.set()
        await release.wait()
        await route.continue_()

    table.add("**/*", waiting)
    router = await enable_router(session, socket, table, failed)
    socket.push({"method": "Fetch.requestPaused", "sessionId": "routing", "params": paused_request()})
    await entered.wait()
    table.clear()
    await router.disable_if_idle(session)
    assert [frame["method"] for frame in socket.sent] == ["Fetch.enable"]
    release.set()
    await socket.wait_for_sent(2)
    assert socket.sent[1]["method"] == "Fetch.continueRequest"
    socket.push({"id": socket.sent[1]["id"], "result": {}})
    await socket.wait_for_sent(3)
    assert socket.sent[2]["method"] == "Fetch.disable"
    socket.push({"id": socket.sent[2]["id"], "result": {}})
    await asyncio.sleep(0.01)
    await router.close(session)
    assert not failed.is_set()


@pytest.mark.parametrize(
    ("scenario", "options"),
    [(_case_fallback_matches_original_url_across_page_and_context_and_keeps_bytes, {})]
    + [(_case_terminal_command_requires_ack_and_survives_caller_cancellation, {})]
    + [
        (_case_rejected_continue_is_failed_closed_instead_of_reported_as_settled, {"code": code, "message": message})
        for code, message in [(-32602, "Invalid parameters"), (-32000, "Fetch domain is not enabled")]
    ]
    + [(_case_cancelling_a_handler_fails_its_paused_request, {})]
    + [(_case_dispatch_errors_cannot_bypass_the_guard, {"failure": failure}) for failure in ["matcher", "abandoned"]]
    + [
        (
            _case_stale_interception_finishes_without_closing_a_healthy_target,
            {"terminal": terminal, "outcomes": outcomes, "closes_target": closes_target},
        )
        for terminal, outcomes, closes_target in [
            ("continue_", ("Invalid InterceptionId.",), False),
            ("fulfill", ("Invalid InterceptionId.",), False),
            ("abort", ("Invalid InterceptionId.",), False),
            ("continue_", ("Invalid parameters", "Invalid InterceptionId."), False),
            ("continue_", ("Invalid parameters", "Invalid parameters"), True),
            ("continue_", ("Invalid parameters", None), False),
        ]
    ]
    + [(_case_missing_terminal_ack_closes_target_without_replaying_or_disabling, {})]
    + [(_case_owner_close_cancels_handlers_without_releasing_other_paused_requests, {})]
    + [(_case_removal_does_not_resume_a_request_before_its_handler_ack, {})],
    ids=lambda value: value.__name__.removeprefix("_case_") if callable(value) else repr(value),
)
@pytest.mark.asyncio
async def test_dispatch_settlement_and_ownership(paused_session, scenario, options) -> None:
    await scenario(paused_session=paused_session, **options)


@pytest.mark.parametrize(
    ("matcher", "url", "expected"),
    [
        (lambda url: url.endswith("favicon.ico"), "https://h.invalid/favicon.ico", True),
        (lambda url: url.endswith("favicon.ico"), "https://h.invalid/index.html", False),
        (re.compile(r"/api/"), "https://h.invalid/api/v1", True),
        (re.compile(r"/api/"), "https://h.invalid/web", False),
    ],
)
def test_callable_and_regex_patterns(matcher, url, expected) -> None:
    assert url_matches(matcher, url) is expected

"""Session bookkeeping when one target carries more than one CDP session.

CDP domain state (Fetch patterns, paused requests, enabled domains) is per *session*, not per
target. Playwright's ``new_cdp_session`` therefore attaches a fresh session so the caller's domain
use cannot collide with the driver's own. This file pins the same contract onto ``CdpConnection``:
a supplementary session coexists with the page's primary session without displacing it.
"""

from __future__ import annotations

import asyncio
import base64
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType, SimpleNamespace

import pytest

from skyvern.webeye.skycdp.connection import CdpConnection, CdpSession
from skyvern.webeye.skycdp.errors import CdpProtocolError, CdpTargetClosedError
from skyvern.webeye.skycdp.facade.browser import Browser, BrowserContext, CdpSessionFacade
from skyvern.webeye.skycdp.facade.page import Page
from skyvern.webeye.skycdp.transport import CdpTransport
from tests.unit.scoped_asyncio import ScopedAsyncio
from tests.unit.test_skycdp_transport import FakeSocket

pytestmark = pytest.mark.asyncio


def eager_completed_gather(*tasks, return_exceptions=False):
    if not all(isinstance(task, asyncio.Future) and task.done() for task in tasks):
        return asyncio.gather(*tasks, return_exceptions=return_exceptions)
    result = asyncio.get_running_loop().create_future()
    values = []
    for task in tasks:
        try:
            values.append(task.result())
        except BaseException as error:
            if not return_exceptions:
                result.set_exception(error)
                return result
            values.append(error)
    result.set_result(values)
    return result


@contextmanager
def retirement_watchdog() -> Iterator[None]:
    caller = asyncio.current_task()

    def expired(_signum: int, _frame: FrameType | None) -> None:
        for task in asyncio.all_tasks():
            task.print_stack()
        while _frame is not None and _frame.f_code.co_name != "_retire":
            _frame = _frame.f_back
        if _frame is None:
            assert caller is not None
            caller.cancel("Retirement probe did not reach the busy loop")
            return
        raise AssertionError("Fetch retirement starved queued completion callbacks")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 2)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


async def _case_public_route_retirement_yields_to_dispatch_completion(monkeypatch, operation: str) -> None:
    connection, socket, primary, _ = await _connection_with_primary_page()
    context = Browser(connection)._context_for(None)
    page = Page(context, primary)
    context._pages.append(page)
    stop = asyncio.get_running_loop().create_future()
    responder = asyncio.create_task(ChromeLikeResponder(socket, "unused").run_until(stop))
    changed, heartbeat = asyncio.Event(), asyncio.Event()
    changes: list[asyncio.Task[None]] = []
    send = primary.send
    scheduled = False

    async def first(route) -> None:
        if operation == "replace":
            await route.fulfill(body="first")
        else:
            await route.abort()

    async def replacement(route) -> None:
        await route.fulfill(body="replacement")

    async def change() -> None:
        await page.unroute("**/api")
        if operation == "replace":
            await page.route("**/api", replacement)
        asyncio.get_running_loop().call_soon(heartbeat.set)
        changed.set()

    async def acknowledged(method: str, params=None, **kwargs):
        nonlocal scheduled
        result = await send(method, params, **kwargs)
        if method in {"Fetch.failRequest", "Fetch.fulfillRequest"} and not scheduled:
            scheduled = True
            asyncio.get_running_loop().call_soon(lambda: changes.append(asyncio.create_task(change())))
        return result

    monkeypatch.setattr(primary, "send", acknowledged)
    await page.route("**/api", first)
    owner = context._fetch._sessions[primary]
    monkeypatch.setattr(sys.modules[type(owner).__module__], "asyncio", ScopedAsyncio(gather=eager_completed_gather))
    try:
        with retirement_watchdog():
            socket.push(
                {
                    "method": "Fetch.requestPaused",
                    "sessionId": primary.session_id,
                    "params": {"requestId": "first", "request": {"url": "https://fixture.invalid/api"}},
                }
            )
            await changed.wait()
            if owner._retiring is not None:
                await owner._retiring
            await asyncio.sleep(0)
            assert heartbeat.is_set() and not owner._tasks
            assert not owner._closing and owner._closing_dispatch is None
            assert sum(frame["method"] == "Fetch.disable" for frame in socket.sent) == (operation == "unroute")
            if operation == "replace":
                before = len(socket.sent)
                socket.push(
                    {
                        "method": "Fetch.requestPaused",
                        "sessionId": primary.session_id,
                        "params": {"requestId": "second", "request": {"url": "https://fixture.invalid/api"}},
                    }
                )
                await socket.wait_for_sent(before + 1)
                await asyncio.gather(*owner._tasks.values())
                assert base64.b64decode(socket.sent[-1]["params"]["body"]) == b"replacement"
            terminals = [
                frame for frame in socket.sent if frame["method"] in {"Fetch.failRequest", "Fetch.fulfillRequest"}
            ]
            assert [frame["params"]["requestId"] for frame in terminals] == (
                ["first", "second"] if operation == "replace" else ["first"]
            )
            assert not owner._tasks and not connection.transport._pending
    finally:
        await asyncio.gather(*changes, return_exceptions=True)
        await context._fetch.close(primary)
        await connection.close()
        stop.cancel()
        responder.cancel()
        await asyncio.gather(responder, return_exceptions=True)
        assert not owner._tasks


async def _case_public_unroute_drains_only_its_own_cohort(
    monkeypatch, scope: str, replace: bool, handler_origin: bool
) -> None:
    connection, socket, primary, _ = await _connection_with_primary_page()
    context = Browser(connection)._context_for(None)
    page = Page(context, primary)
    context._pages.append(page)
    routes = page if scope == "page" else context
    stop = asyncio.get_running_loop().create_future()
    responder = asyncio.create_task(ChromeLikeResponder(socket, "unused").run_until(stop))
    entered, release_handler, removed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first_sending, first_ack, release_ack = asyncio.Event(), asyncio.Event(), asyncio.Event()
    new_entered, release_new = asyncio.Event(), asyncio.Event()
    late_entered, release_late, disabled, heartbeat = (asyncio.Event() for _ in range(4))
    commands: list[tuple[str, str | None]] = []
    send = primary.send

    async def first(route) -> None:
        entered.set()
        await release_handler.wait()
        if handler_origin:
            await routes.unroute("**/api")
            removed.set()
        await route.fulfill(body="first")

    async def replacement(route) -> None:
        new_entered.set()
        await release_new.wait()
        await route.fulfill(body="replacement")

    async def gated_send(method, params=None, **kwargs):
        request_id = (params or {}).get("requestId")
        if method == "Fetch.fulfillRequest" and request_id == "first":
            first_sending.set()
            await release_ack.wait()
        if method == "Fetch.continueRequest" and request_id == "late":
            late_entered.set()
            await release_late.wait()
        result = await send(method, params, **kwargs)
        commands.append((method, request_id))
        if method == "Fetch.fulfillRequest" and request_id == "first":
            first_ack.set()
        if method == "Fetch.disable":
            disabled.set()
        return result

    def pause(request_id, path):
        socket.push(
            {
                "method": "Fetch.requestPaused",
                "sessionId": primary.session_id,
                "params": {"requestId": request_id, "request": {"url": "https://fixture.invalid/" + path}},
            }
        )

    monkeypatch.setattr(primary, "send", gated_send)
    await routes.route("**/api", first)
    owner = context._fetch._sessions[primary]
    waiting = asyncio.create_task(disabled.wait())
    try:
        pause("first", "api")
        await asyncio.wait_for(entered.wait(), 1)
        if handler_origin:
            release_handler.set()
            await asyncio.wait_for(removed.wait(), 1)
        else:
            await routes.unroute("**/api")
        if replace:
            await routes.route("**/api", replacement)
            pause("replacement", "api")
            await asyncio.wait_for(new_entered.wait(), 1)
            await routes.unroute("**/api")
        pause("late", "unmatched")
        await asyncio.wait_for(late_entered.wait(), 1)
        release_handler.set()
        await asyncio.wait_for(first_sending.wait(), 1)
        assert not disabled.is_set(), "Fetch.disable preceded the original handler's terminal ACK"
        release_ack.set()
        await asyncio.wait_for(first_ack.wait(), 1)
        if replace:
            done, _ = await asyncio.wait([waiting], timeout=0.02)
            assert not done, "Obsolete retirement disabled the replacement's active handler"
            release_new.set()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        done, _ = await asyncio.wait([waiting], timeout=1)
        assert done, "Later default continuations extended the completed removal cohort"
        assert heartbeat.is_set() and not owner._tasks["late"].done()
        assert commands.index(("Fetch.fulfillRequest", "first")) < commands.index(("Fetch.disable", None))
        release_late.set()
        await asyncio.gather(*owner._tasks.values())
        await owner._retiring
        assert not owner._tasks and not owner._closing and not connection.transport._pending
        terminals = [request_id for method, request_id in commands if method.startswith("Fetch.") and request_id]
        assert sorted(terminals) == (["first", "late", "replacement"] if replace else ["first", "late"])
        assert commands.count(("Fetch.disable", None)) == 1
        assert not any(frame["method"] == "Target.closeTarget" for frame in socket.sent)
    finally:
        release_handler.set()
        release_ack.set()
        release_new.set()
        release_late.set()
        await context._fetch.close(primary)
        await connection.close()
        waiting.cancel()
        stop.cancel()
        responder.cancel()
        await asyncio.gather(waiting, responder, return_exceptions=True)
        assert not owner._tasks


async def _case_concurrent_handlers_can_close_the_same_page(
    monkeypatch, close_mode: str, signal: str, close_owner: str
) -> None:
    connection, socket, primary, _ = await _connection_with_primary_page()
    context = Browser(connection)._context_for(None)
    page = Page(context, primary)
    context._pages.append(page)
    stop = asyncio.get_running_loop().create_future()
    responder = asyncio.create_task(ChromeLikeResponder(socket, "unused").run_until(stop))
    entered = 0
    both_entered, closed = asyncio.Event(), asyncio.Event()

    async def close_page(route) -> None:
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await both_entered.wait()
        close = page.close if close_owner == "page" else context.close
        if close_mode == "task":
            await asyncio.create_task(close())
        elif close_mode == "gather":
            await asyncio.gather(close())
        else:
            await close()
        closed.set()

    owner = None
    try:
        await page.route("**/*", close_page)
        owner = context._fetch._sessions[primary]
        cancel_tasks = owner._cancel_tasks

        def interrupt_drain() -> None:
            if not owner._closing:
                # Deliver the transport event before cancelled dispatchers resume.
                if signal == "detach":
                    socket.push({"method": "Target.detachedFromTarget", "params": {"sessionId": primary.session_id}})
                elif signal == "disconnect":
                    socket.drop()
            cancel_tasks()

        monkeypatch.setattr(owner, "_cancel_tasks", interrupt_drain)
        for request_id in ("first", "second"):
            socket.push(
                {
                    "method": "Fetch.requestPaused",
                    "sessionId": primary.session_id,
                    "params": {"requestId": request_id, "request": {"url": "https://fixture.test/blocked"}},
                }
            )
        await asyncio.wait_for(closed.wait(), 1)
        if owner._tasks:
            _, unfinished = await asyncio.wait(list(owner._tasks.values()), timeout=1)
            assert not unfinished
        assert entered == 2 and page.is_closed()
        assert sum(frame["method"] == "Target.closeTarget" for frame in socket.sent) == (signal != "disconnect")
        assert not any(frame["method"] == "Fetch.continueRequest" for frame in socket.sent)
        assert not owner._tasks
        assert not context._fetch._sessions
        assert not connection.transport._pending
        if close_owner == "context":
            assert context._closed
        if signal != "none":
            assert primary.detached
    finally:
        pending = [
            task
            for task in [
                page._close_task,
                getattr(context, "_close_task", None),
                *(owner._tasks.values() if owner else ()),
            ]
            if task
        ]
        await connection.close()
        for task in pending:
            task.cancel()
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=1)
            assert not unfinished
        stop.cancel()
        responder.cancel()
        await asyncio.gather(responder, return_exceptions=True)


async def _case_external_close_drains_a_handler_rejoining_the_same_close(
    monkeypatch, close_owner: str, close_mode: str, registration: str | None = None
) -> None:
    connection, socket, primary, _ = await _connection_with_primary_page()
    context = Browser(connection)._context_for(None)
    page = Page(context, primary)
    context._pages.append(page)
    stop = asyncio.get_running_loop().create_future()
    responder = asyncio.create_task(ChromeLikeResponder(socket, "unused").run_until(stop))
    close = context.close if close_owner == "context" else page.close
    entered = asyncio.Event()
    draining, release_registration = asyncio.Event(), asyncio.Event()
    installer = None

    async def handler(route) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            if registration is not None:
                draining.set()
                await release_registration.wait()
                await context.route("**/replacement", handler)
            elif close_mode == "task":
                await asyncio.create_task(close())
            elif close_mode == "gather":
                await asyncio.gather(close())
            else:
                await close()

    closing = None
    dispatcher = None
    try:
        await page.route("**/guarded", handler)
        owner = context._fetch._sessions[primary]
        socket.push(
            {
                "method": "Fetch.requestPaused",
                "sessionId": primary.session_id,
                "params": {"requestId": "guarded", "request": {"url": "https://fixture.invalid/guarded"}},
            }
        )
        await asyncio.wait_for(entered.wait(), 1)
        dispatcher = owner._tasks["guarded"]
        closing = asyncio.create_task(close())
        if registration is not None:
            await asyncio.wait_for(draining.wait(), 1)
            if registration == "locked":
                installer = asyncio.create_task(context.route("**/independent", handler))
                for _ in range(100):
                    if context._route_lock.locked():
                        break
                    await asyncio.sleep(0)
                assert context._route_lock.locked()
            release_registration.set()
        _, unfinished = await asyncio.wait({closing}, timeout=1)
        assert not unfinished, "External close is waiting for the handler rejoining that same close"
        await closing
        if installer is not None:
            await asyncio.wait_for(installer, 1)
        assert dispatcher.cancelled() and dispatcher.cancelling() == 1
        assert page not in context.pages and page.is_closed()
        assert sum(frame["method"] == "Target.closeTarget" for frame in socket.sent) == 1
        assert not any(frame["method"] == "Fetch.continueRequest" for frame in socket.sent)
        assert not owner._tasks and not context._fetch._sessions and not connection.transport._pending
        if close_owner == "context":
            assert context._closed
    finally:
        release_registration.set()
        pending = {
            task for task in (dispatcher, closing, installer, page._close_task, context._close_task) if task is not None
        }
        # A second cancellation is teardown only, after the outcome assertion above.
        for task in pending:
            if not task.done():
                task.cancel()
        await connection.close()
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=1)
            assert not unfinished
        stop.cancel()
        responder.cancel()
        await asyncio.gather(responder, return_exceptions=True)


async def _case_rejected_context_close_preserves_routing_and_can_be_retried(monkeypatch, created_context: bool) -> None:
    connection, socket, primary, _ = await _connection_with_primary_page()
    browser = Browser(connection)
    if created_context:
        creating = asyncio.create_task(browser.new_context())
        await socket.wait_for_sent(len(socket.sent) + 1)
        socket.push({"id": socket.sent[-1]["id"], "result": {"browserContextId": "created"}})
        context = await creating
    else:
        context = browser._context_for(None)
    page = Page(context, primary)
    context._pages.append(page)
    callers: list[asyncio.Task[None]] = []

    async def join_close() -> asyncio.Task[None]:
        entered = asyncio.Event()

        async def close() -> None:
            entered.set()
            await context.close()

        caller = asyncio.create_task(close())
        callers.append(caller)
        await entered.wait()
        return caller

    try:

        async def guarded(route) -> None:
            await route.fulfill(body="guarded")

        installing = asyncio.create_task(context.route("**/*", guarded))
        await socket.wait_for_sent(len(socket.sent) + 1)
        socket.push({"id": socket.sent[-1]["id"], "result": {}})
        await installing
        before = len(socket.sent)
        closing = await join_close()
        await socket.wait_for_sent(before + 1)
        frame = socket.sent[-1]
        assert frame["method"] == "Target.closeTarget"
        joining = await join_close()
        assert not joining.done(), "A concurrent caller returned before the close attempt completed"
        socket.push({"id": frame["id"], "error": {"code": -32602, "message": "Invalid parameters"}})
        results = await asyncio.wait_for(asyncio.gather(closing, joining, return_exceptions=True), 1)
        assert all(isinstance(result, CdpProtocolError) for result in results)
        assert results[0] is results[1]
        assert page in context.pages
        assert not page.is_closed()
        assert not context._closed
        assert not any(frame["method"] == "Target.disposeBrowserContext" for frame in socket.sent)

        socket.push(
            {
                "method": "Fetch.requestPaused",
                "sessionId": primary.session_id,
                "params": {"requestId": "after-rejection", "request": {"url": "https://example.invalid/guarded"}},
            }
        )
        await socket.wait_for_sent(before + 2)
        assert socket.sent[-1]["method"] == "Fetch.fulfillRequest"
        socket.push({"id": socket.sent[-1]["id"], "result": {}})

        retry = await join_close()
        await socket.wait_for_sent(before + 3)
        joining_retry = await join_close()
        assert not retry.done() and not joining_retry.done()
        socket.push({"id": socket.sent[-1]["id"], "result": {"success": True}})
        if created_context:
            await socket.wait_for_sent(before + 4)
            assert socket.sent[-1]["method"] == "Target.disposeBrowserContext"
            assert not retry.done() and not joining_retry.done()
            socket.push({"id": socket.sent[-1]["id"], "result": {}})
        await asyncio.wait_for(asyncio.gather(retry, joining_retry), 1)
        assert page not in context.pages
        assert page.is_closed()
        assert context._closed
        assert sum(frame["method"] == "Target.closeTarget" for frame in socket.sent) == 2
    finally:
        await connection.close()
        if callers:
            _, unfinished = await asyncio.wait(callers, timeout=1)
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*callers, return_exceptions=True)
            assert not unfinished


async def _case_old_page_close_waiter_cannot_clear_an_in_progress_retry(
    monkeypatch, retry_rejected: bool, cancel_old_waiter: bool
) -> None:
    connection, socket, primary, _ = await _connection_with_primary_page()
    context = Browser(connection)._context_for(None)
    page = Page(context, primary)
    context._pages.append(page)
    callers: list[asyncio.Task[None]] = []
    closed: list[Page] = []
    page.on("close", closed.append)

    async def close_then_retry() -> None:
        with pytest.raises(CdpProtocolError):
            await page.close()
        await page.close()

    try:
        before = len(socket.sent)
        retrying = asyncio.create_task(close_then_retry())
        callers.append(retrying)
        await socket.wait_for_sent(before + 1)
        first_command = socket.sent[-1]
        assert first_command["method"] == "Target.closeTarget"
        old_waiter = asyncio.create_task(page.close())
        callers.append(old_waiter)
        await asyncio.sleep(0)
        if cancel_old_waiter:
            old_waiter.cancel()
            await asyncio.sleep(0)
        socket.push({"id": first_command["id"], "error": {"code": -32602, "message": "Invalid parameters"}})
        await socket.wait_for_sent(before + 2)
        retry_command = socket.sent[-1]
        assert retry_command["method"] == "Target.closeTarget"
        old_result = (await asyncio.wait_for(asyncio.gather(old_waiter, return_exceptions=True), 1))[0]
        assert isinstance(old_result, asyncio.CancelledError if cancel_old_waiter else CdpProtocolError)

        joining_retry = asyncio.create_task(page.close())
        callers.append(joining_retry)
        await asyncio.sleep(0)
        assert not joining_retry.done(), "A late caller returned while the actual retry was still unresolved"
        assert page._close_task is not None and not page._close_task.done()
        assert len(socket.sent) == before + 2
        reply = (
            {"error": {"code": -32602, "message": "Invalid parameters"}}
            if retry_rejected
            else {"result": {"success": True}}
        )
        socket.push({"id": retry_command["id"], **reply})
        results = await asyncio.wait_for(asyncio.gather(retrying, joining_retry, return_exceptions=True), 1)
        if retry_rejected:
            assert all(isinstance(result, CdpProtocolError) for result in results)
            assert results[0] is results[1]
            assert page in context.pages and not page.is_closed() and not closed
        else:
            assert results == [None, None]
            assert page not in context.pages and page.is_closed() and closed == [page]
        assert page._close_task is None
        assert not connection.transport._pending
    finally:
        await connection.close()
        for caller in callers:
            if not caller.done():
                caller.cancel()
        if callers:
            _, unfinished = await asyncio.wait(callers, timeout=1)
            await asyncio.gather(*callers, return_exceptions=True)
            assert not unfinished


async def _case_cancelling_a_context_close_waiter_preserves_the_shared_cleanup(monkeypatch) -> None:
    connection, socket, primary, _ = await _connection_with_primary_page()
    context = Browser(connection)._context_for(None)
    page = Page(context, primary)
    context._pages.append(page)
    closing = asyncio.create_task(context.close())
    try:
        await socket.wait_for_sent(len(socket.sent) + 1)
        frame = socket.sent[-1]
        assert frame["method"] == "Target.closeTarget"
        closing.cancel()
        joining = asyncio.create_task(context.close())
        socket.push({"id": frame["id"], "result": {"success": True}})
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 1)
        await asyncio.wait_for(joining, 1)
        assert page.is_closed() and not context.pages and context._closed
        assert sum(frame["method"] == "Target.closeTarget" for frame in socket.sent) == 1
    finally:
        await connection.close()
        await asyncio.gather(closing, return_exceptions=True)


async def _two_page_context():
    connection, socket, primary, _ = await _connection_with_primary_page()
    socket.push(
        {
            "method": "Target.attachedToTarget",
            "params": {"sessionId": "S-other", "targetInfo": {"targetId": "T2", "type": "page", "url": "about:blank"}},
        }
    )
    await asyncio.sleep(0)
    context = BrowserContext(SimpleNamespace(connection=connection), "owned")
    context._pages.extend([Page(context, primary), Page(context, connection.sessions["S-other"])])
    return connection, socket, context


async def _case_context_route_failure_rolls_back_and_retry_guards_all_live_pages(
    monkeypatch, failed_page, concurrent, recovery=None
) -> None:
    baseline = asyncio.all_tasks()
    connection, socket, context = await _two_page_context()
    failed = context.pages[failed_page] if failed_page is not None else None
    stop = asyncio.get_running_loop().create_future()
    reject = True
    enabling = asyncio.Event()
    held = []
    close_held = []
    retrying = asyncio.Event()
    first_enables = 0

    def reply(frame):
        nonlocal first_enables
        if frame["method"] == "Fetch.enable" and frame.get("sessionId") == context._pages[0].session.session_id:
            first_enables += 1
            if first_enables == 2:
                retrying.set()
        if recovery is not None and frame["method"] == "Target.closeTarget":
            close_held.append(frame)
            return None
        if (
            concurrent
            and frame["method"] == "Fetch.enable"
            and not held
            and (recovery is None or frame.get("sessionId") == failed.session.session_id)
        ):
            held.append(frame)
            enabling.set()
            return None
        if (
            frame["method"] == "Fetch.enable"
            and failed
            and frame.get("sessionId") == failed.session.session_id
            and reject
        ):
            return {"error": {"code": -32602, "message": "Invalid parameters"}}
        return {"result": {}}

    responder = asyncio.create_task(ChromeLikeResponder(socket, "unused", reply).run_until(stop))

    async def handler(route) -> None:
        await route.fulfill(body="guarded")

    async def newer(route) -> None:
        await route.fulfill(body="newer")

    installers = []
    try:
        if concurrent:
            installers.append(asyncio.create_task(context.route("**/guarded", handler)))
            await asyncio.wait_for(enabling.wait(), 1)
            installers.append(asyncio.create_task(context.route("**/guarded", newer)))
            await asyncio.sleep(0)
            frame = held[0]
            reject = False
            socket.push(
                {
                    "id": frame["id"],
                    **(
                        {"error": {"code": -32602, "message": "Invalid parameters"}}
                        if failed_page == 0 or recovery is not None
                        else {"result": {}}
                    ),
                }
            )
            if recovery is not None:
                await asyncio.wait_for(retrying.wait(), 1)
                assert failed.is_closed() and close_held
                closing = failed._close_task
                frame = close_held[0]
                socket.push(
                    {
                        "id": frame["id"],
                        **(
                            {"error": {"code": -32602, "message": "Invalid parameters"}}
                            if recovery == "rejected"
                            else {"result": {}}
                        ),
                    }
                )
                closed = await asyncio.gather(closing, return_exceptions=True)
                assert isinstance(closed[0], CdpProtocolError) if recovery == "rejected" else closed == [None]
            outcomes = await asyncio.wait_for(asyncio.gather(*installers, return_exceptions=True), 1)
            if recovery is not None:
                await context.route("**/guarded", newer)
                assert (failed in context.pages) == (recovery == "rejected")
            assert outcomes[1] is None
            assert all(
                page.session in context._fetch._sessions and context._fetch._sessions[page.session]._enabled
                for page in context.pages
            ), "A concurrent registration returned success with an unguarded live page"
            assert [entry.handler for entry in context.routes.matching("https://fixture.invalid/guarded")] == (
                [newer, newer] if recovery is not None else [newer] if failed_page == 0 else [newer, handler]
            )
            assert (
                isinstance(outcomes[0], CdpProtocolError)
                if failed_page == 0 or recovery is not None
                else outcomes[0] is None
            )
            close_held.clear()
            recovery = None
            await context.close()
            return
        if failed:
            with pytest.raises(CdpProtocolError):
                await context.route("**/guarded", handler)
            assert len(context.routes) == 0, "A rejected registration left a partial context guard installed"
            await failed.close()
            assert all(not owner._enabled for owner in context._fetch._sessions.values())
        reject = False
        await context.route("**/guarded", handler)
        assert all(context._fetch._sessions[page.session]._enabled for page in context.pages)
        await context.route("**/newer", handler)
        assert next(context.routes.matching("https://fixture.invalid/newer")).handler is handler
        await context.close()
        assert not context.pages and not context._fetch._sessions and not connection.transport._pending
    finally:
        await connection.close()
        stop.cancel()
        responder.cancel()
        await asyncio.gather(
            responder,
            *installers,
            *(page._close_task for page in context._pages if page._close_task),
            return_exceptions=True,
        )
        await asyncio.sleep(0)
        assert not (asyncio.all_tasks() - baseline)


async def _case_owned_context_teardown_joins_a_page_close_from_inherited_dispatch(
    monkeypatch, interference: bool
) -> None:
    baseline = asyncio.all_tasks()
    connection, socket, context = await _two_page_context()
    first, guarded = context.pages
    stop = asyncio.get_running_loop().create_future()
    first_close, abort = asyncio.Event(), asyncio.Event()
    held = {}

    def reply(frame):
        if frame["method"] == "Target.closeTarget" and frame["params"]["targetId"] == first.session.target.target_id:
            held["first"] = frame
            first_close.set()
            return None
        if frame["method"] == "Fetch.failRequest":
            held["abort"] = frame
            abort.set()
            return None
        return {"result": {}}

    responder = asyncio.create_task(ChromeLikeResponder(socket, "unused", reply).run_until(stop))
    callers = []
    dispatcher = None

    async def handler(route) -> None:
        await context.close()
        await route.abort()

    try:
        await guarded.route("**/guarded", handler)
        owner = context._fetch._sessions[guarded.session]
        socket.push(
            {
                "method": "Fetch.requestPaused",
                "sessionId": guarded.session.session_id,
                "params": {"requestId": "guarded", "request": {"url": "https://fixture.invalid/guarded"}},
            }
        )
        await asyncio.wait_for(first_close.wait(), 1)
        dispatcher = owner._tasks["guarded"]
        joining = asyncio.create_task(context.close())
        callers.append(joining)
        if interference:
            callers.append(asyncio.create_task(guarded.close()))
            await asyncio.wait_for(abort.wait(), 1)
        socket.push({"id": held["first"]["id"], "result": {"success": True}})
        if interference:
            done, _ = await asyncio.wait([joining], timeout=0.02)
            assert not done, "An uncancelled context joiner inherited the draining dispatch rejection"
            socket.push({"id": held["abort"]["id"], "result": {}})
        await asyncio.wait_for(asyncio.gather(*callers), 1)
        if not interference:
            await asyncio.wait_for(abort.wait(), 1)
            socket.push({"id": held["abort"]["id"], "result": {}})
        await asyncio.gather(dispatcher, return_exceptions=True)
        assert context._closed and not context.pages
        assert sum(frame["method"] == "Target.disposeBrowserContext" for frame in socket.sent) == 1
        assert sum(frame["method"] == "Target.closeTarget" for frame in socket.sent) == 2
        assert not owner._tasks and not connection.transport._pending
    finally:
        for frame in held.values():
            socket.push({"id": frame["id"], "result": {}})
        pending = {
            task
            for task in [*callers, dispatcher, context._close_task, *(page._close_task for page in context._pages)]
            if task is not None
        }
        await connection.close()
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        stop.cancel()
        responder.cancel()
        await asyncio.gather(responder, return_exceptions=True)
        await asyncio.sleep(0)
        assert not (asyncio.all_tasks() - baseline)


async def _case_rejected_target_close_retains_failed_drain_and_target_only_retry(monkeypatch, settlement: str) -> None:
    baseline = asyncio.all_tasks()
    connection, socket, primary, _ = await _connection_with_primary_page()
    context = BrowserContext(SimpleNamespace(connection=connection), "owned")
    page = Page(context, primary)
    context._pages.append(page)
    stop = asyncio.get_running_loop().create_future()
    entered = asyncio.Event()
    attempts = 0
    connection.transport._default_timeout = 0.03

    def reply(frame):
        nonlocal attempts
        if frame["method"] == "Fetch.failRequest" and settlement != "accepted":
            return {"error": {"code": -32602, "message": "Invalid parameters"}} if settlement == "rejected" else None
        if frame["method"] == "Target.closeTarget":
            attempts += 1
            if attempts == 1:
                return {"error": {"code": -32602, "message": "Invalid parameters"}}
        return {"result": {}}

    responder = asyncio.create_task(ChromeLikeResponder(socket, "unused", reply).run_until(stop))

    async def handler(route) -> None:
        entered.set()
        await asyncio.Event().wait()

    try:
        await page.route("**/guarded", handler)
        owner = context._fetch._sessions[primary]
        socket.push(
            {
                "method": "Fetch.requestPaused",
                "sessionId": primary.session_id,
                "params": {"requestId": "guarded", "request": {"url": "https://fixture.invalid/guarded"}},
            }
        )
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(CdpProtocolError):
            await page.close()
        if settlement == "accepted":
            await page.route("**/fresh", handler)
            assert not owner._closing
        else:
            with pytest.raises(CdpTargetClosedError):
                await page.route("**/fresh", handler)
            assert owner._closing, "An unresolved drain was restored to normal interception"
        await page.close()
        assert page.is_closed() and not context.pages and attempts == 2
        assert [frame["params"]["requestId"] for frame in socket.sent if frame["method"] == "Fetch.failRequest"] == [
            "guarded"
        ]
        assert not owner._tasks and not context._fetch._sessions and not connection.transport._pending
    finally:
        await connection.close()
        stop.cancel()
        responder.cancel()
        await asyncio.gather(responder, return_exceptions=True)
        await asyncio.sleep(0)
        assert not (asyncio.all_tasks() - baseline)


class ChromeLikeResponder:
    """Answer every command frame, emitting ``Target.attachedToTarget`` BEFORE the command result
    for attach commands — the order the real browser uses, and the order that trips a connection
    which registers attach results only in the response path."""

    def __init__(self, socket: FakeSocket, next_session_id: str, reply=None) -> None:
        self._socket = socket
        self._next_session_id = next_session_id
        self._answered: set[int] = set()
        self.attach_frames: list[dict] = []
        self._reply = reply

    async def run_until(self, predicate: asyncio.Future) -> None:
        while not predicate.done():
            for frame in list(self._socket.sent):
                frame_id = frame.get("id")
                if frame_id is None or frame_id in self._answered:
                    continue
                self._answered.add(frame_id)
                if self._reply is not None:
                    response = self._reply(frame)
                    if response is not None:
                        self._socket.push({"id": frame_id, **response})
                    continue
                if frame["method"] == "Target.attachToTarget":
                    self.attach_frames.append(frame)
                    self._socket.push(
                        {
                            "method": "Target.attachedToTarget",
                            "params": {
                                "sessionId": self._next_session_id,
                                "targetInfo": {
                                    "targetId": frame["params"]["targetId"],
                                    "type": "page",
                                    "url": "https://example.com",
                                },
                            },
                        }
                    )
                    self._socket.push({"id": frame_id, "result": {"sessionId": self._next_session_id}})
                else:
                    self._socket.push({"id": frame_id, "result": {}})
            await asyncio.sleep(0)


async def _connection_with_primary_page() -> tuple[CdpConnection, FakeSocket, CdpSession, list[CdpSession]]:
    socket = FakeSocket()
    transport = CdpTransport(socket)
    await transport.start()
    connection = CdpConnection(transport)

    announced: list[CdpSession] = []
    connection.on_page_session(announced.append)

    started = asyncio.ensure_future(connection.start())
    responder = ChromeLikeResponder(socket, next_session_id="unused")
    await responder.run_until(started)
    await started

    socket.push(
        {
            "method": "Target.attachedToTarget",
            "params": {
                "sessionId": "S-primary",
                "targetInfo": {"targetId": "T1", "type": "page", "url": "https://example.com"},
            },
        }
    )
    settle = asyncio.ensure_future(asyncio.sleep(0.05))
    await ChromeLikeResponder(socket, next_session_id="unused").run_until(settle)
    await settle

    primary = connection.sessions["S-primary"]
    return connection, socket, primary, announced


async def _case_supplementary_attach_returns_a_distinct_session(monkeypatch) -> None:
    connection, socket, primary, _ = await _connection_with_primary_page()
    try:
        pending = asyncio.ensure_future(connection.attach_supplementary("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-supp").run_until(pending)
        supplementary = await pending

        assert supplementary.session_id == "S-supp"
        assert supplementary is not primary
    finally:
        await connection.close()


async def test_supplementary_attach_does_not_displace_the_primary_session() -> None:
    connection, socket, primary, announced = await _connection_with_primary_page()
    try:
        pending = asyncio.ensure_future(connection.attach_supplementary("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-supp").run_until(pending)
        await pending

        # The page's own session must stay the one attach() resolves, or every later caller
        # (frames, evaluation, teardown) silently migrates onto the supplementary session.
        resolved = asyncio.ensure_future(connection.attach("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-wrong").run_until(resolved)
        assert (await resolved) is primary

        # And a supplementary session is raw access, not a page: announcing it would make the
        # context build a duplicate Page facade for a target that already has one.
        assert announced == [primary]
    finally:
        await connection.close()


async def test_dropping_the_supplementary_session_keeps_the_primary_mapping() -> None:
    connection, socket, primary, _ = await _connection_with_primary_page()
    try:
        pending = asyncio.ensure_future(connection.attach_supplementary("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-supp").run_until(pending)
        supplementary = await pending

        socket.push({"method": "Target.detachedFromTarget", "params": {"sessionId": "S-supp"}})
        settle = asyncio.ensure_future(asyncio.sleep(0.05))
        await ChromeLikeResponder(socket, next_session_id="unused").run_until(settle)
        await settle

        assert supplementary.detached
        assert not primary.detached
        resolved = asyncio.ensure_future(connection.attach("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-wrong").run_until(resolved)
        assert (await resolved) is primary
    finally:
        await connection.close()


async def test_new_cdp_session_attaches_a_dedicated_session_for_the_caller() -> None:
    # The call-site pin for the SKY-14066 fix itself: without it, every other test here stays green
    # while new_cdp_session hands back the page's own session and the engine's route dispatcher
    # settles the caller's paused Fetch requests. Browser-free on purpose — the conformance test
    # that also covers this needs a real Chromium the gating CI arm does not have.
    connection, socket, primary, _ = await _connection_with_primary_page()
    try:
        context = BrowserContext(browser=SimpleNamespace(connection=connection), browser_context_id=None)
        page_stub = SimpleNamespace(session=primary)
        pending = asyncio.ensure_future(context.new_cdp_session(page_stub))
        await ChromeLikeResponder(socket, next_session_id="S-supp").run_until(pending)
        facade = await pending

        assert any(
            frame["method"] == "Target.attachToTarget" and frame["params"]["targetId"] == "T1" for frame in socket.sent
        )
        assert facade._session is not primary
        assert facade._session.session_id == "S-supp"
    finally:
        await connection.close()


@pytest.mark.parametrize(
    ("browser_context_id", "scope"),
    [("ctx-1", {"browserContextId": "ctx-1"}), (None, {})],
    ids=["created_context", "default_context"],
)
async def test_cookie_calls_act_on_the_facade_context_not_the_default_one(
    browser_context_id: str | None, scope: dict[str, str]
) -> None:
    # Storage.* without browserContextId acts on the browser's default context, so a run in a created
    # context would read, set, or clear someone else's cookie jar and report success.
    sent: list[tuple[str, dict | None]] = []

    async def send(method: str, params: dict | None = None) -> dict:
        sent.append((method, params))
        return {"cookies": []}

    browser = SimpleNamespace(connection=SimpleNamespace(transport=SimpleNamespace(send=send)))
    context = BrowserContext(browser=browser, browser_context_id=browser_context_id)
    cookie = {"name": "session", "value": "x", "url": "https://portal.example/"}

    await context.clear_cookies()
    await context.add_cookies([cookie])
    await context.cookies("https://portal.example/")

    assert sent == [
        ("Storage.clearCookies", scope),
        ("Storage.setCookies", {**scope, "cookies": [cookie]}),
        ("Storage.getCookies", {**scope, "urls": ["https://portal.example/"]}),
    ]


async def test_target_destruction_reaps_supplementary_sessions_too() -> None:
    # _target_sessions holds only the primary, so reaping by the mapping alone leaves a
    # supplementary session alive in bookkeeping and on the transport's subscriber index after
    # its target is gone.
    connection, socket, primary, _ = await _connection_with_primary_page()
    try:
        pending = asyncio.ensure_future(connection.attach_supplementary("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-supp").run_until(pending)
        supplementary = await pending

        socket.push({"method": "Target.targetDestroyed", "params": {"targetId": "T1"}})
        settle = asyncio.ensure_future(asyncio.sleep(0.05))
        await ChromeLikeResponder(socket, next_session_id="unused").run_until(settle)
        await settle

        assert primary.detached
        assert supplementary.detached
        assert "S-supp" not in connection.sessions
    finally:
        await connection.close()


async def test_session_facade_detach_releases_the_supplementary_session() -> None:
    connection, socket, _, _ = await _connection_with_primary_page()
    try:
        pending = asyncio.ensure_future(connection.attach_supplementary("T1"))
        await ChromeLikeResponder(socket, next_session_id="S-supp").run_until(pending)
        supplementary = await pending

        facade = CdpSessionFacade(connection, session=supplementary)
        detached = asyncio.ensure_future(facade.detach())
        await ChromeLikeResponder(socket, next_session_id="unused").run_until(detached)
        await detached

        assert any(
            frame["method"] == "Target.detachFromTarget" and frame["params"] == {"sessionId": "S-supp"}
            for frame in socket.sent
        )
    finally:
        await connection.close()


@pytest.mark.parametrize(
    ("scenario", "options"),
    [
        (_case_public_route_retirement_yields_to_dispatch_completion, {"operation": operation})
        for operation in ["unroute", "replace"]
    ]
    + [
        (
            _case_public_unroute_drains_only_its_own_cohort,
            {"scope": scope, "replace": replace, "handler_origin": handler_origin},
        )
        for scope in ["page", "context"]
        for replace in [False, True]
        for handler_origin in [False, True]
    ]
    + [
        (
            _case_concurrent_handlers_can_close_the_same_page,
            {"close_mode": close_mode, "signal": signal, "close_owner": close_owner},
        )
        for close_mode in ["direct", "task", "gather"]
        for signal in ["none", "detach", "disconnect"]
        for close_owner in ["page", "context"]
    ]
    + [
        (
            _case_external_close_drains_a_handler_rejoining_the_same_close,
            {"close_owner": close_owner, "close_mode": close_mode},
        )
        for close_owner in ["page", "context"]
        for close_mode in ["direct", "task", "gather"]
    ]
    + [
        (
            _case_external_close_drains_a_handler_rejoining_the_same_close,
            {"close_owner": close_owner, "close_mode": "direct", "registration": registration},
        )
        for close_owner in ["page", "context"]
        for registration in ["direct", "locked"]
    ]
    + [
        (_case_rejected_context_close_preserves_routing_and_can_be_retried, {"created_context": created_context})
        for created_context in [False, True]
    ]
    + [
        (
            _case_old_page_close_waiter_cannot_clear_an_in_progress_retry,
            {"retry_rejected": retry_rejected, "cancel_old_waiter": cancel_old_waiter},
        )
        for retry_rejected in [False, True]
        for cancel_old_waiter in [False, True]
    ]
    + [(_case_cancelling_a_context_close_waiter_preserves_the_shared_cleanup, {})]
    + [
        (
            _case_context_route_failure_rolls_back_and_retry_guards_all_live_pages,
            {"failed_page": failed_page, "concurrent": concurrent},
        )
        for failed_page in [None, 0, 1]
        for concurrent in [False, True]
    ]
    + [
        (
            _case_context_route_failure_rolls_back_and_retry_guards_all_live_pages,
            {"failed_page": 1, "concurrent": True, "recovery": recovery},
        )
        for recovery in ["rejected", "accepted"]
    ]
    + [
        (_case_owned_context_teardown_joins_a_page_close_from_inherited_dispatch, {"interference": interference})
        for interference in [False, True]
    ]
    + [
        (_case_rejected_target_close_retains_failed_drain_and_target_only_retry, {"settlement": settlement})
        for settlement in ["accepted", "rejected", "unknown"]
    ]
    + [(_case_supplementary_attach_returns_a_distinct_session, {})],
    ids=lambda value: value.__name__.removeprefix("_case_") if callable(value) else repr(value),
)
async def test_context_and_session_lifecycle(monkeypatch, scenario, options) -> None:
    await scenario(monkeypatch=monkeypatch, **options)

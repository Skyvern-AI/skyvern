from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from playwright.async_api import BrowserContext, CDPSession, Page

from skyvern.webeye.cdp_errors import CdpError, CdpProtocolError, CdpTargetClosedError, CdpTimeoutError, protocol_error
from skyvern.webeye.cdp_fetch import FetchRouter
from skyvern.webeye.http_routing import RouteHandler, RouteTable, URLMatcher


class _SdkSession:
    def __init__(self, session: CDPSession, page: Page) -> None:
        self.session = session
        self.page = page
        self.closed = False
        self._cache_disabled = False
        session.on("close", self._closed)

    def _closed(self, *_: Any) -> None:
        self.closed = True
        self._cache_disabled = False

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict:
        if method == "Fetch.enable":
            await self._set_cache_disabled(True)
            try:
                return await self._command(method, params)
            except CdpProtocolError:
                await self._set_cache_disabled(False)
                raise
        result = await self._command(method, params)
        if method == "Fetch.disable":
            await self._set_cache_disabled(False)
        return result

    async def _set_cache_disabled(self, disabled: bool) -> None:
        if self._cache_disabled != disabled:
            if disabled:
                await self._command("Network.enable")
            await self._command("Network.setCacheDisabled", {"cacheDisabled": disabled})
            self._cache_disabled = disabled

    async def _command(self, method: str, params: dict[str, Any] | None = None) -> dict:
        try:
            async with asyncio.timeout(30):
                return await self.session.send(method, params)
        except CdpError:
            raise
        except TimeoutError as error:
            raise CdpTimeoutError(f"{method}: no acknowledgment before deadline") from error
        except Exception as error:
            if self.closed or self.page.is_closed():
                raise CdpTargetClosedError(f"{method}: session closed") from error
            prefix = f"CDPSession.send: Protocol error ({method}): "
            message = str(error)
            if message.startswith(prefix):
                raise protocol_error(method, {"message": message.removeprefix(prefix)}) from error
            raise CdpError(f"{method}: SDK command outcome unknown") from error

    def on(self, event: str, handler: Callable[[dict], None]) -> None:
        self.session.on(event, handler)

    def off(self, event: str, handler: Callable[[dict], None]) -> None:
        self.session.remove_listener(event, handler)

    async def detach(self) -> None:
        if not self.closed:
            try:
                failure: BaseException | None = None
                try:
                    async with asyncio.timeout(30):
                        await self._set_cache_disabled(False)
                except BaseException as error:
                    failure = error
                try:
                    async with asyncio.timeout(30):
                        await self.session.detach()
                        self._cache_disabled = False
                finally:
                    if failure is not None:
                        raise failure
            except Exception:
                if not self.closed and not self.page.is_closed():
                    raise


@dataclass
class _PageBinding:
    session: _SdkSession
    routes: RouteTable
    on_close: Callable[..., None]


class HttpInterception:
    def __init__(self, context: BrowserContext) -> None:
        self.context = context
        self._router = FetchRouter(RouteTable())
        self._pages: dict[Page, _PageBinding] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        context.on("close", self._context_closed)

    def _page_closed(self, page: Page) -> None:
        binding = self._pages.pop(page, None)
        if binding is not None:
            self._router.detached(binding.session)
            page.remove_listener("close", binding.on_close)

    def _context_closed(self, *_: Any) -> None:
        self._closed = True
        for page in list(self._pages):
            self._page_closed(page)

    async def route_page(self, page: Page, matcher: URLMatcher, handler: RouteHandler) -> None:
        async with self._lock:
            if self._closed:
                raise CdpTargetClosedError("HTTP interception owner is closed")
            binding = self._pages.get(page)
            if binding is None:
                session = _SdkSession(await self.context.new_cdp_session(page), page)

                def on_close(*_: Any) -> None:
                    self._page_closed(page)

                binding = _PageBinding(session, RouteTable(), on_close)
                self._pages[page] = binding
                page.on("close", on_close)
            binding.routes.add(matcher, handler)
            await self._router.enable(binding.session, binding.routes, page.close)

    async def unroute_page(self, page: Page, matcher: URLMatcher) -> None:
        async with self._lock:
            binding = self._pages.get(page)
            if binding is not None:
                binding.routes.remove(matcher)
                await self._router.disable_if_idle(binding.session)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="http-interception-close")
        closing = self._close_task
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await asyncio.shield(closing)
            raise
        finally:
            if closing.done():
                self._close_task = None

    async def _close(self) -> None:
        async with self._lock:
            error: Exception | None = None
            try:
                for page, binding in list(self._pages.items()):
                    binding.routes.clear()
                    try:
                        await self._router.release(binding.session, binding.session.detach)
                    except Exception as failure:
                        if error is None:
                            error = failure
                    else:
                        self._page_closed(page)
            finally:
                self._closed = True
                self.context.remove_listener("close", self._context_closed)
            if error is not None:
                raise error


def http_interception(context: BrowserContext) -> HttpInterception:
    owner: HttpInterception | None = getattr(context, "_skyvern_http_interception", None)
    if owner is None or owner._closed:
        owner = HttpInterception(context)
        context._skyvern_http_interception = owner  # type: ignore[attr-defined]
    return owner

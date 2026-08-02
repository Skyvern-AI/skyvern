"""Classify every navigation hop before the browser connects to it, over the CDP Fetch domain.

``navigate_with_retry`` validates the destination before dispatch and revalidates the followed
chain after ``page.goto`` returns, but a redirect the browser already followed cannot be undone
by then: the internal request was issued and its response scripts may have run. This guard closes
that window by classifying each hop while it is paused.

Why CDP ``Fetch`` and not Playwright routing (probed on Chromium 128, playwright 1.4x):
``context.route("**/*")`` does **not** re-fire on a 3xx target (playwright#3993/#34994) — a handler
on a ``/entry -> /hop1 -> /final`` chain sees only ``/entry`` while the browser follows the rest.
Reaching the hops through routing therefore requires re-issuing every request via
``route.fetch(max_redirects=0)``, which serves the response from Playwright's own network stack:
that drops all four ``Sec-Fetch-*`` metadata headers and reorders the rest, and (because
``route.fulfill`` terminates the chain) starves the co-registered header route in
``browser_factory._apply_origin_scoped_headers``. ``Fetch.requestPaused`` re-fires on every hop
with the browser doing the fetching, so neither cost applies.

Scope and limits:
- Pauses ``Document`` requests only, so subresources are untouched; sub-frame navigations are
  ``Document`` requests and are covered.
- Classification is ``validate_navigation_destination`` — deliberately the same function used
  before dispatch and after return, so the three checks cannot drift apart.
- That classifier does not resolve DNS, so a public hostname with an internal A record still
  passes, and a rebinding host can flip between check and connect regardless. Both need
  enforcement below the browser (network policy); no userspace guard closes them.
- Installation is best-effort: a browser that refuses a CDP session keeps only the pre-dispatch
  and post-return checks rather than losing navigation entirely.
- **Popups are NOT covered.** Playwright exposes a popup ``Page`` only after its initial
  navigation has begun, so ``window.open`` / ``target=_blank`` straight to an internal URL wins
  the race against the install (probed: the internal body reached the popup). The only userspace
  fix is a Playwright ``context.route``, which closes it — but registering any route makes
  Chromium disable its HTTP cache and add ``Pragma``/``Cache-Control: no-cache`` to every
  request, and routes are already disallowed on some runs, so it is neither free nor universal.
  Popup egress belongs to below-browser network policy, alongside DNS rebinding.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from playwright.async_api import BrowserContext, CDPSession, Page

from skyvern.exceptions import BlockedNavigationDestination
from skyvern.webeye.navigation import validate_navigation_destination

LOG = structlog.get_logger()

_GUARD_ATTR = "_skyvern_navigation_egress_guard"

# requestStage Request pauses the hop before it leaves the browser; resourceType Document keeps
# the pause count at roughly one per navigation instead of one per subresource.
_FETCH_PATTERNS: list[dict[str, str]] = [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]


class NavigationEgressGuard:
    def __init__(self, cdp_session: CDPSession) -> None:
        self._cdp_session = cdp_session
        self._tasks: set[asyncio.Task[None]] = set()

    def on_request_paused(self, event: dict[str, Any]) -> None:
        task = asyncio.create_task(self._decide(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _decide(self, event: dict[str, Any]) -> None:
        request_id = event.get("requestId")
        if not request_id:
            return
        url = (event.get("request") or {}).get("url") or ""

        try:
            validate_navigation_destination(url)
        except BlockedNavigationDestination as error:
            LOG.warning("Blocked navigation hop to an internal destination", url=url, reason=error.reason)
            await self._answer("Fetch.failRequest", {"requestId": request_id, "errorReason": "AddressUnreachable"})
            return
        except Exception:
            # A paused request that never receives a verdict hangs the navigation until its
            # timeout, so an unexpected classifier failure still has to answer — and blocking
            # is the only answer that cannot leak an internal response.
            LOG.exception("Navigation egress classification failed; blocking the hop", url=url)
            await self._answer("Fetch.failRequest", {"requestId": request_id, "errorReason": "AddressUnreachable"})
            return

        await self._answer("Fetch.continueRequest", {"requestId": request_id})

    async def _answer(self, method: str, params: dict[str, Any]) -> None:
        try:
            await self._cdp_session.send(method, params)
        except Exception:
            # The session detaches when the page closes and the pause dies with it; a lost verdict
            # on an already-gone request is expected, not a failure to enforce.
            LOG.debug("Navigation egress verdict could not be delivered", method=method, exc_info=True)


async def install_navigation_egress_guard(page: Page) -> None:
    """Attach the per-hop guard to ``page`` once; subsequent calls are no-ops."""
    if getattr(page, _GUARD_ATTR, None) is not None:
        return

    try:
        cdp_session = await page.context.new_cdp_session(page)
        guard = NavigationEgressGuard(cdp_session)
        cdp_session.on("Fetch.requestPaused", guard.on_request_paused)
        await cdp_session.send("Fetch.enable", {"patterns": _FETCH_PATTERNS})
    except Exception:
        LOG.warning(
            "Could not install the per-hop navigation egress guard; "
            "navigation keeps only pre-dispatch and post-return validation",
            exc_info=True,
        )
        return

    page._skyvern_navigation_egress_guard = guard  # type: ignore[attr-defined]


def arm_navigation_egress_guard(browser_context: BrowserContext) -> None:
    """Arm every page of ``browser_context``, including ones the site opens itself.

    Binding the guard to a navigation helper leaves anything that does not call one unguarded:
    popups, ``target=_blank``, and generated scripts, which acquire the browser with
    ``navigate=False`` and then drive ``page.goto`` directly. Binding it to the context does not.

    The page event schedules the install, so a page that navigates in the same tick can still
    outrun it; the navigation entry points also await ``install_navigation_egress_guard``, which
    is idempotent, to close that race where a caller exists.

    Best-effort by construction: this runs inside browser-context creation, where any raise
    becomes UnknownErrorWhileCreatingBrowserContext and fails the whole launch. A guard layered
    on top of two existing validators must never be able to do that.
    """
    pending: set[asyncio.Task[None]] = set()

    def on_page(page: Page) -> None:
        task = asyncio.create_task(install_navigation_egress_guard(page))
        pending.add(task)
        task.add_done_callback(pending.discard)

    try:
        browser_context.on("page", on_page)
        for page in browser_context.pages:
            on_page(page)
    except Exception:
        LOG.warning(
            "Could not arm the navigation egress guard on the browser context; "
            "navigation entry points still install it per page",
            exc_info=True,
        )

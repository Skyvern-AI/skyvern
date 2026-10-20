import asyncio
import contextlib
import json

import structlog
from playwright.async_api import Page

from skyvern.forge import app
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.schemas.browser_settings import (
    BrowserSettingsReceipt,
    BrowserSettingsSource,
    BrowserSettingsStatus,
    build_timezone_receipt,
    iana_timezone_keys,
    requested_timezone_id,
)

LOG = structlog.get_logger()

_PROBE_TIMEOUT_SECONDS = 5
_TIMEZONE_PROBE = """((requested) => {
  let browserNameForRequested = null;
  try {
    browserNameForRequested = new Intl.DateTimeFormat("en-US", { timeZone: requested }).resolvedOptions().timeZone;
  } catch (e) {}
  return [Intl.DateTimeFormat().resolvedOptions().timeZone, browserNameForRequested];
})"""


async def _probe_in_isolated_world(page: Page, requested: str) -> object:
    cdp_session = await page.context.new_cdp_session(page)
    try:
        frame_tree = await cdp_session.send("Page.getFrameTree")
        isolated_world = await cdp_session.send(
            "Page.createIsolatedWorld",
            {"frameId": frame_tree["frameTree"]["frame"]["id"], "worldName": "skyvern-timezone-receipt"},
        )
        result = await cdp_session.send(
            "Runtime.evaluate",
            {
                "expression": f"{_TIMEZONE_PROBE}({json.dumps(requested)})",
                "contextId": isolated_world["executionContextId"],
                "returnByValue": True,
            },
        )
        return None if "exceptionDetails" in result else result["result"].get("value")
    finally:
        with contextlib.suppress(Exception):
            await cdp_session.detach()


async def measure_timezone_receipt(
    page: Page | None, requested: str, source: BrowserSettingsSource
) -> BrowserSettingsReceipt:
    """Measures in a fresh isolated world, so a site script cannot change the answer."""
    value: object = None
    if page is not None:
        try:
            value = await asyncio.wait_for(_probe_in_isolated_world(page, requested), _PROBE_TIMEOUT_SECONDS)
        except Exception:
            LOG.warning("Could not measure the browser timezone", exc_info=True)
    reported, named = value if isinstance(value, list) and len(value) == 2 else (None, None)
    browser_name_for_requested = named if isinstance(named, str) else None
    # Only an IANA name, or the browser's own name for the request, counts as a measurement.
    actual = (
        reported
        if isinstance(reported, str) and (reported in iana_timezone_keys() or reported == browser_name_for_requested)
        else None
    )
    return build_timezone_receipt(requested, actual, source, browser_name_for_requested=browser_name_for_requested)


def is_final_receipt(receipt: BrowserSettingsReceipt | None) -> bool:
    return receipt is not None and receipt.status != BrowserSettingsStatus.unknown


async def record_session_timezone_receipt(session: PersistentBrowserSession, page: Page | None) -> None:
    """The page must belong to the browser this session launched; a later measurement may only replace unknown."""
    requested = requested_timezone_id(session.browser_settings)
    if requested is None or is_final_receipt(session.browser_settings_receipt):
        return
    source = (
        BrowserSettingsSource.workflow_version
        if session.created_for_workflow_run_id
        else BrowserSettingsSource.session_request
    )
    receipt = await measure_timezone_receipt(page, requested, source)
    try:
        await app.DATABASE.browser_sessions.record_persistent_browser_session_browser_settings_receipt(
            session.persistent_browser_session_id, session.organization_id, receipt
        )
    except Exception:
        LOG.warning(
            "Failed to record the browser session timezone receipt",
            browser_session_id=session.persistent_browser_session_id,
            exc_info=True,
        )

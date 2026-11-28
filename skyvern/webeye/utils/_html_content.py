from __future__ import annotations

import asyncio
import re
import secrets
from typing import TYPE_CHECKING, cast
from weakref import ReferenceType, ref

import structlog
from playwright.async_api import CDPSession, Frame, Page

from skyvern.forge import app
from skyvern.webeye.action_deadline import cancellable_driver_call, raise_if_cancelled
from skyvern.webeye.browser_engine import BrowserEngineSelection
from skyvern.webeye.browser_object_predicates import is_page_like

if TYPE_CHECKING:
    from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()
_ADOPTED_OWNER = "_bounded_html_owner"
# JSON escaping can take six bytes per UTF-16 unit, leaving headroom below a 256 MiB receiver.
CHUNK_THRESHOLD_UTF16_UNITS = 32 * 1024 * 1024
SLICE_UNITS = 65_536
SNAPSHOT_JS = """(() => ({
    html: (document.doctype ? new XMLSerializer().serializeToString(document.doctype) : "") +
          (document.documentElement ? document.documentElement.outerHTML : "")
}))()"""
SLICE_JS = """function(offset, size) {
    const html = this.html;
    let end = Math.min(offset + size, html.length);
    if (end < html.length && html.charCodeAt(end - 1) >= 0xd800 && html.charCodeAt(end - 1) <= 0xdbff &&
        html.charCodeAt(end) >= 0xdc00 && html.charCodeAt(end) <= 0xdfff) end--;
    return {text: html.slice(offset, end), end, units: html.length};
}"""


def bind_adopted_bounded_html_transport(owner: BrowserState) -> None:
    # The managed factory has provenance even when its driver classes differ from the reader's imports.
    if owner.browser_context is not None:
        setattr(owner.browser_context, _ADOPTED_OWNER, ref(owner))


def _normalize_surrogates(text: str) -> str:
    # Match the driver's replacement of lone UTF-16 surrogates in native content replies.
    if re.search(r"[\ud800-\udfff]", text):
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return text


async def _snapshot_value(session: CDPSession, object_id: str, function: str) -> object:
    reply = await cancellable_driver_call(
        lambda: session.send(
            "Runtime.callFunctionOn",
            {"objectId": object_id, "functionDeclaration": function, "returnByValue": True},
        )
    )
    if reply.get("exceptionDetails"):
        raise RuntimeError("HTML snapshot evaluation failed")
    return reply.get("result", {}).get("value")


async def _read_chunks(session: CDPSession, object_id: str, total: int) -> str:
    parts: list[str] = []
    offset = 0
    while True:

        async def send_slice(offset: int = offset) -> dict:
            return await session.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": SLICE_JS,
                    "arguments": [{"value": offset}, {"value": SLICE_UNITS}],
                    "returnByValue": True,
                },
            )

        reply = await cancellable_driver_call(send_slice)
        value = reply.get("result", {}).get("value")
        if reply.get("exceptionDetails") or not isinstance(value, dict):
            raise RuntimeError("HTML slice evaluation failed")
        text, end, units = value.get("text"), value.get("end"), value.get("units")
        if (
            not isinstance(text, str)
            or type(end) is not int
            or type(units) is not int
            or not offset <= end <= min(offset + SLICE_UNITS, units)
            or (end == offset and end != units)
            or total != units
        ):
            raise RuntimeError("Invalid HTML slice bounds")
        text = _normalize_surrogates(text)
        if len(text.encode("utf-16-le")) // 2 != end - offset:
            raise RuntimeError("Invalid HTML slice length")
        parts.append(text)
        if end == units:
            raise_if_cancelled()
            return "".join(parts)
        offset = end


async def _read_sized_content(page: Page) -> str:
    group = "html-content-" + secrets.token_hex(16)
    attachment = asyncio.create_task(page.context.new_cdp_session(page))
    snapshot: asyncio.Task[dict] | None = None

    async def release() -> None:
        try:
            session = attachment.result()
            try:
                if snapshot is not None:
                    if not snapshot.cancelled():
                        snapshot.exception()
                    async with asyncio.timeout(0.4):
                        await session.send("Runtime.releaseObjectGroup", {"objectGroup": group})
            finally:
                async with asyncio.timeout(0.4):
                    await session.detach()
        except (Exception, asyncio.CancelledError) as exc:
            LOG.debug("HTML CDP cleanup failed", error_type=type(exc).__name__)

    try:
        session = await asyncio.shield(attachment)
        tree = await cancellable_driver_call(lambda: session.send("Page.getFrameTree"))
        frame_id = tree["frameTree"]["frame"]["id"]
        world = await cancellable_driver_call(
            lambda: session.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "html-content"})
        )
        snapshot = asyncio.create_task(
            session.send(
                "Runtime.evaluate",
                {
                    "expression": SNAPSHOT_JS,
                    "contextId": world["executionContextId"],
                    "objectGroup": group,
                    "returnByValue": False,
                },
            )
        )
        reply = await asyncio.shield(snapshot)
        object_id = reply.get("result", {}).get("objectId")
        if reply.get("exceptionDetails") or not isinstance(object_id, str) or not object_id:
            raise RuntimeError("HTML snapshot acquisition failed")
        total = await _snapshot_value(session, object_id, "function() { return this.html.length; }")
        if type(total) is not int or total < 0:
            raise RuntimeError("Invalid HTML snapshot length")
        if total >= CHUNK_THRESHOLD_UTF16_UNITS:
            return await _read_chunks(session, object_id, total)
        # Read the sized snapshot itself so a later DOM change cannot enlarge this reply.
        html = await _snapshot_value(session, object_id, "function() { return this.html; }")
        if not isinstance(html, str):
            raise RuntimeError("Invalid HTML snapshot content")
        return _normalize_surrogates(html)
    finally:
        # Release after the last reply that can allocate a session or object, even if it arrives late.
        pending = snapshot if snapshot is not None else attachment
        if pending.done():
            await asyncio.shield(asyncio.create_task(release()))
        else:
            pending.add_done_callback(lambda _: asyncio.create_task(release()))


async def read_html(target: Page | Frame, selection: BrowserEngineSelection | None) -> str:
    if not is_page_like(target):
        return await target.content()
    page = cast(Page, target)
    context = page.context
    adopted = getattr(context, _ADOPTED_OWNER, None) if selection is None else None
    if selection is None:
        if not isinstance(adopted, ReferenceType):
            return await page.content()
    elif not selection.metadata.supports_bounded_html:
        return await page.content()

    def require_current() -> None:
        if adopted is not None:
            owner = adopted()
            if owner is None or owner.browser_context is not context or page.context is not context:
                raise RuntimeError("Adopted browser context ownership changed")

    require_current()
    browser = context.browser
    # A persistent context has no owning Browser, but still exposes a CDP
    # session through context.new_cdp_session(). Only a non-chromium owning
    # browser rules out the bounded transport.
    if browser is not None and browser.browser_type.name != "chromium":
        return await page.content()
    enabled = await app.AGENT_FUNCTION.html_content_bounded_transport_enabled()
    require_current()
    raise_if_cancelled()
    source = await _read_sized_content(page) if enabled else await page.content()
    require_current()
    raise_if_cancelled()
    return source

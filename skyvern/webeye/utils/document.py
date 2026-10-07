import asyncio
from typing import Any

import structlog

LOG = structlog.get_logger()

# CDPSession calls take no timeout of their own, and on an unresponsive target they never return.
LOADER_ID_ATTACH_TIMEOUT_SECONDS = 5
LOADER_ID_FRAME_TREE_TIMEOUT_SECONDS = 5
LOADER_ID_DETACH_TIMEOUT_SECONDS = 2


async def get_main_document_loader_id(page: Any) -> str | None:
    """Read Chromium's main-frame loader id using a short-lived CDP session; None when it cannot be read in time."""
    # asyncio.timeout rather than wait_for: on Python 3.11 wait_for can drop a cancellation that races the reply.
    raw_page = getattr(page, "page", page)
    try:
        async with asyncio.timeout(LOADER_ID_ATTACH_TIMEOUT_SECONDS):
            session = await raw_page.context.new_cdp_session(raw_page)
    except TimeoutError:
        LOG.warning("Timed out reading the main document loader id", cdp_call="new_cdp_session")
        return None
    except Exception:
        return None
    loader_id: str | None = None
    send_cancelled: asyncio.CancelledError | None = None
    try:
        async with asyncio.timeout(LOADER_ID_FRAME_TREE_TIMEOUT_SECONDS):
            tree = await session.send("Page.getFrameTree")
        candidate = tree.get("frameTree", {}).get("frame", {}).get("loaderId")
        loader_id = candidate if isinstance(candidate, str) else None
    except asyncio.CancelledError as exc:
        send_cancelled = exc
    except TimeoutError:
        LOG.warning("Timed out reading the main document loader id", cdp_call="Page.getFrameTree")
        loader_id = None
    except Exception:
        loader_id = None
    try:
        # Bounded even while a cancellation is pending: detach on the same dead target would otherwise
        # hold that cancellation forever.
        async with asyncio.timeout(LOADER_ID_DETACH_TIMEOUT_SECONDS):
            await session.detach()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            LOG.warning("Timed out reading the main document loader id", cdp_call="detach")
        if send_cancelled is None:
            return None
    if send_cancelled is not None:
        raise send_cancelled
    return loader_id
